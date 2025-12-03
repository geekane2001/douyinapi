import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Any
from playwright.async_api import async_playwright, BrowserContext

# ==========================================================
# 0. 日志配置
# ==========================================================
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("LBS_Proxy")

def log_json_preview(title: str, data: Any, max_len: int = 2000):
    """打印 JSON 预览，max_len 设置大一点以便看到完整响应"""
    try:
        text = json.dumps(data, ensure_ascii=False)
        if len(text) > max_len:
            logger.info(f"{title}: {text[:max_len]}... (剩余 {len(text)-max_len} 字符)")
        else:
            logger.info(f"{title}: {text}")
    except:
        logger.info(f"{title}: [无法序列化数据]")

# ==========================================================
# 1. Pydantic 数据模型
# ==========================================================

class Fence(BaseModel):
    poi_id: str
    radius: int
    center_lng: Optional[float] = None
    center_lat: Optional[float] = None

class Time(BaseModel):
    raw_text: str 

class AweTypeCode(BaseModel):
    code: str
    level: int

class GeneralPayload(BaseModel):
    entity_type: int = 1
    entity_ids: Optional[List[str]] = [] 
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

class PortraitPayload(BaseModel):
    awe_poi_id: str
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode
    entity_type: int = 1

# ==========================================================
# 2. 全局变量与生命周期
# ==========================================================

playwright_instance = None
browser_context: Optional[BrowserContext] = None
AUTH_FILE = "auth.json"

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser_context
    logger.info("🚀 [System] 服务启动中...")
    
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True, 
        args=['--no-sandbox', '--disable-setuid-sandbox']
    )
    
    try:
        logger.info(f"📂 [System] 加载 Cookie: {AUTH_FILE}")
        browser_context = await browser.new_context(storage_state=AUTH_FILE)
        logger.info("✅ [System] 浏览器上下文就绪")
    except Exception as e:
        logger.warning(f"⚠️ [System] 加载 Cookie 失败: {e}")
        browser_context = await browser.new_context()

    yield
    
    logger.info("🛑 [System] 服务关闭中...")
    if browser_context: await browser_context.close()
    if playwright_instance: await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

# ==========================================================
# 3. 核心请求工具函数
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'

URL_TOPK_PRODUCTS = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
URL_TOPK_STORES   = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/pois/v2'
URL_PORTRAIT      = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'

def build_payload(origin: GeneralPayload, etype: int, specific_ids: List[str] = None):
    fence_data = {
        "poi_id": origin.locsight_fence.poi_id,
        "radius": origin.locsight_fence.radius
    }
    
    # 检查经纬度
    if origin.locsight_fence.center_lng and origin.locsight_fence.center_lat:
        fence_data["center_lng"] = origin.locsight_fence.center_lng
        fence_data["center_lat"] = origin.locsight_fence.center_lat
    else:
        # 再次强调警告
        logger.warning(f"⚠️ [Payload] 警告: 缺少经纬度 (center_lng/lat)，POI: {origin.locsight_fence.poi_id}。这可能导致 '实体列表为空' 错误。")

    data = {
        "entity_type": etype,
        "locsight_fence": fence_data,
        "locsight_time": origin.locsight_time.dict(),
        "awe_type_code": origin.awe_type_code.dict()
    }
    
    if specific_ids is not None:
        data["entity_ids"] = specific_ids

    return data

async def fetch_api_in_browser(page, url, payload, tag="API"):
    """执行浏览器 fetch"""
    payload_str = json.dumps(payload, ensure_ascii=False)
    logger.info(f"⚡ [{tag}] Request -> {url.split('?')[0]}")
    logger.info(f"📦 [{tag}] Request Body -> {payload_str}")

    js_code = f"""
        async (payload) => {{
            try {{
                const response = await fetch('{url}', {{
                    method: 'POST',
                    headers: {{ 'content-type': 'application/json', 'user': '{USER_ID}' }},
                    body: JSON.stringify(payload)
                }});
                const text = await response.text();
                try {{
                    return {{ status: response.status, json: JSON.parse(text) }};
                }} catch (e) {{
                    return {{ status: response.status, error: 'JSON Parse Error', text: text }};
                }}
            }} catch(e) {{ 
                return {{ status: -1, error: e.toString() }}; 
            }}
        }}
    """
    result = await page.evaluate(js_code, payload)
    
    # --- 【新增】详细响应日志 ---
    status = result.get("status")
    json_data = result.get("json")
    
    if status == 200 and json_data:
        # 成功拿到 JSON，打印出来
        log_json_preview(f"📄 [{tag}] Response OK", json_data)
    elif result.get("text"):
        # 拿到文本但不是 JSON (可能是 HTML 报错页)
        logger.error(f"❌ [{tag}] Response (Raw Text): {result.get('text')[:500]}")
    elif result.get("error"):
        # 网络错误或 JS 错误
        logger.error(f"❌ [{tag}] Browser Error: {result.get('error')}")
    else:
        logger.error(f"❌ [{tag}] Unknown Error: {result}")

    return result

# ==========================================================
# 4. 接口路由
# ==========================================================

@app.post("/topk")
async def get_topk_data(payload: GeneralPayload):
    if not browser_context: raise HTTPException(503, "Service not ready")
    page = await browser_context.new_page()
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        url = f"{URL_TOPK_STORES}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        clean_data = build_payload(payload, etype=1, specific_ids=None)
        
        # 强制清除 entity_ids
        clean_data.pop("entity_ids", None)
        
        result = await fetch_api_in_browser(page, url, clean_data, tag="TopK-Stores")
        
        if result.get("status") == 200:
            return result.get("json")
        else:
            raise HTTPException(500, f"Upstream Error: {result}")
    finally:
        await page.close()

@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    if not browser_context: raise HTTPException(503)
    page = await browser_context.new_page()
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        url = f"{URL_PORTRAIT}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        result = await fetch_api_in_browser(page, url, payload.dict(), tag="Portrait")
        
        if result.get("status") == 200:
            return result.get("json")
        else:
            raise HTTPException(500, f"Portrait Error: {result}")
    finally:
        await page.close()

@app.post("/products")
async def get_products_data(payload: GeneralPayload):
    if not browser_context: raise HTTPException(503, "Service not ready")
    page = await browser_context.new_page()
    
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        # Step 1
        url_step1 = f"{URL_TOPK_PRODUCTS}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        payload_step1 = build_payload(payload, etype=2, specific_ids=None)
        payload_step1.pop("entity_ids", None)
        
        res1 = await fetch_api_in_browser(page, url_step1, payload_step1, tag="Step1-Products")
        products_list = res1.get("json", {}).get("data", {}).get("product_operation", [])
        
        if not products_list:
            logger.warning("   ⚠️ Step 1 无商品，结束")
            return {"code": 0, "message": "success (no products)", "data": []}

        # Step 2
        product_ids = [str(p['product_id']) for p in products_list]
        url_step2 = f"{URL_TOPK_STORES}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        payload_step2 = build_payload(payload, etype=2, specific_ids=product_ids)
        
        res2 = await fetch_api_in_browser(page, url_step2, payload_step2, tag="Step2-Relations")
        stores_list = res2.get("json", {}).get("data", {}).get("pois", [])

        # Step 3
        product_map = {str(p['product_id']): p for p in products_list}
        final_result = []
        for store in stores_list:
            store_obj = {
                "awe_poi_id": store.get('awe_poi_id'),
                "name": store.get('name'),
                "product_details": []
            }
            for pid in store.get('related_entity_ids', []):
                pid_str = str(pid)
                if pid_str in product_map:
                    store_obj['product_details'].append(product_map[pid_str])
            
            if len(store_obj['product_details']) > 0:
                final_result.append(store_obj)

        log_json_preview("🎉 组装结果", final_result, max_len=500)
        return { "code": 0, "message": "success", "data": final_result }

    except Exception as e:
        logger.error(f"❌ [Error] {e}")
        return {"code": -1, "message": str(e), "data": []}
    finally:
        await page.close()

@app.get("/")
def read_root():
    return {"status": "ok", "routes": ["/topk", "/products", "/portrait"]}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
