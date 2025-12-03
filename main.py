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

def log_json_preview(title: str, data: Any, max_len: int = 1000):
    try:
        text = json.dumps(data, ensure_ascii=False)
        if len(text) > max_len:
            logger.info(f"{title}: {text[:max_len]}... (剩余 {len(text)-max_len} 字符)")
        else:
            logger.info(f"{title}: {text}")
    except:
        pass

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
# 3. 核心请求工具
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'

# 【关键】URL 定义
# v2: 排行榜接口 (不需要 IDs)
URL_RANKING = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
# pois/v2: 详情接口 (需要 IDs 或 精确坐标)
URL_DETAILS = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/pois/v2'
# portrait: 画像接口
URL_PORTRAIT = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'

def build_strict_payload(origin: GeneralPayload, etype: int, specific_ids: List[str] = None):
    """构造请求体，严格剔除无效字段"""
    fence_data = {
        "poi_id": origin.locsight_fence.poi_id,
        "radius": origin.locsight_fence.radius
    }
    
    # 仅当经纬度存在时添加
    if origin.locsight_fence.center_lng and origin.locsight_fence.center_lat:
        fence_data["center_lng"] = origin.locsight_fence.center_lng
        fence_data["center_lat"] = origin.locsight_fence.center_lat

    data = {
        "entity_type": etype,
        "locsight_fence": fence_data,
        "locsight_time": origin.locsight_time.dict(),
        "awe_type_code": origin.awe_type_code.dict()
    }
    
    # 仅当 IDs 不为 None 时添加
    if specific_ids is not None:
        data["entity_ids"] = specific_ids

    return data

async def fetch_api_in_browser(page, url, payload, tag="API"):
    """执行浏览器 fetch"""
    logger.info(f"⚡ [{tag}] Request -> {url.split('?')[0]}")
    logger.info(f"📦 [{tag}] Body -> {json.dumps(payload, ensure_ascii=False)}")

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
                    return {{ status: response.status, error: 'JSON Parse Error', text: text.substring(0, 500) }};
                }}
            }} catch(e) {{ 
                return {{ status: -1, error: e.toString() }}; 
            }}
        }}
    """
    return await page.evaluate(js_code, payload)

# ==========================================================
# 4. 接口路由
# ==========================================================

# --- 1. 获取门店列表 (Competitors) ---
@app.post("/topk")
async def get_topk_data(payload: GeneralPayload):
    """
    获取竞品门店列表。
    【修正】：使用 URL_RANKING (.../topk/v2)，而不是 .../pois/v2
    """
    logger.info(f"📥 [Endpoint] /topk (门店列表) - Radius: {payload.locsight_fence.radius}")
    
    if not browser_context: raise HTTPException(503)
    page = await browser_context.new_page()
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        # 修正 URL：使用排行榜接口，它不需要 entity_ids
        url = f"{URL_RANKING}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        
        data = build_strict_payload(payload, etype=1, specific_ids=None)
        
        result = await fetch_api_in_browser(page, url, data, tag="TopK-StoreList")
        
        if result.get("status") == 200:
            json_res = result.get("json", {})
            if json_res.get("code") != 0:
                logger.error(f"❌ [TopK Error] Code: {json_res.get('code')}, Msg: {json_res.get('message')}")
            else:
                count = len(json_res.get("data", {}).get("poi_operation", []))
                logger.info(f"✅ [TopK Success] 找到 {count} 个门店")
            return json_res
        else:
            raise HTTPException(500, f"Upstream Error: {result}")
    finally:
        await page.close()

# --- 2. 获取用户画像 ---
@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    if not browser_context: raise HTTPException(503)
    page = await browser_context.new_page()
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        url = f"{URL_PORTRAIT}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        # 画像 Payload 清洗
        data = payload.dict()
        if not data['locsight_fence'].get('center_lng'):
            data['locsight_fence'].pop('center_lng', None)
            data['locsight_fence'].pop('center_lat', None)

        return (await fetch_api_in_browser(page, url, data, tag="Portrait")).get("json")
    finally:
        await page.close()

# --- 3. 获取商品套餐 (串行组装) ---
@app.post("/products")
async def get_products_data(payload: GeneralPayload):
    logger.info(f"📥 [Endpoint] /products - Radius: {payload.locsight_fence.radius}")
    
    if not browser_context: raise HTTPException(503)
    page = await browser_context.new_page()
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        # === Step 1: 查商品 (排行榜接口) ===
        url1 = f"{URL_RANKING}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        data1 = build_strict_payload(payload, etype=2, specific_ids=None)
        
        res1 = await fetch_api_in_browser(page, url1, data1, tag="Step1-Products")
        products_list = res1.get("json", {}).get("data", {}).get("product_operation", [])
        
        if not products_list:
            logger.warning("   ⚠️ 无商品数据，结束")
            return {"code": 0, "message": "success", "data": []}

        # === Step 2: 查关联 (详情接口，带 IDs) ===
        pids = [str(p['product_id']) for p in products_list]
        url2 = f"{URL_DETAILS}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        data2 = build_strict_payload(payload, etype=2, specific_ids=pids)
        
        res2 = await fetch_api_in_browser(page, url2, data2, tag="Step2-Relations")
        stores_list = res2.get("json", {}).get("data", {}).get("pois", [])

        # === Step 3: 组装 ===
        product_map = {str(p['product_id']): p for p in products_list}
        final_result = []
        for store in stores_list:
            obj = {
                "awe_poi_id": store.get('awe_poi_id'),
                "name": store.get('name'),
                "product_details": []
            }
            for pid in store.get('related_entity_ids', []):
                if str(pid) in product_map:
                    obj['product_details'].append(product_map[str(pid)])
            
            if obj['product_details']:
                final_result.append(obj)

        logger.info(f"🎉 组装完成: {len(final_result)} 门店")
        log_json_preview("📦 返回数据", final_result, 500)
        return {"code": 0, "message": "success", "data": final_result}

    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return {"code": -1, "message": str(e), "data": []}
    finally:
        await page.close()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
