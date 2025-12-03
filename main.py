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
# 0. 高级日志配置
# ==========================================================
# 设置日志格式，显示时间、级别和具体消息
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("LBS_Proxy")

def log_json_preview(title: str, data: Any, max_len: int = 500):
    """辅助函数：打印 JSON 预览，防止日志过长刷屏"""
    try:
        text = json.dumps(data, ensure_ascii=False)
        if len(text) > max_len:
            logger.info(f"{title}: {text[:max_len]}... (共 {len(text)} 字符)")
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
    
    # 生产环境配置
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

# API 地址
URL_TOPK_PRODUCTS = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
URL_TOPK_STORES   = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/pois/v2'
URL_PORTRAIT      = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'

def build_payload(origin: GeneralPayload, etype: int, specific_ids: List[str] = None):
    """构造请求体"""
    data = {
        "entity_type": etype,
        "locsight_fence": {
            "poi_id": origin.locsight_fence.poi_id,
            "radius": origin.locsight_fence.radius
        },
        "locsight_time": origin.locsight_time.dict(),
        "awe_type_code": origin.awe_type_code.dict()
    }
    
    if specific_ids is not None:
        data["entity_ids"] = specific_ids
        
    if origin.locsight_fence.center_lng:
        data["locsight_fence"]["center_lng"] = origin.locsight_fence.center_lng
    if origin.locsight_fence.center_lat:
        data["locsight_fence"]["center_lat"] = origin.locsight_fence.center_lat

    return data

async def fetch_api_in_browser(page, url, payload, tag="API"):
    """执行浏览器 fetch 并详细记录日志"""
    logger.info(f"⚡ [{tag}] Request -> {url.split('?')[0]}")
    # logger.debug(f"📦 [{tag}] Payload: {json.dumps(payload, ensure_ascii=False)}") # 调试时可解开

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
                    return {{ status: response.status, error: 'JSON Parse Error', text: text.substring(0, 200) }};
                }}
            }} catch(e) {{ 
                return {{ status: -1, error: e.toString() }}; 
            }}
        }}
    """
    result = await page.evaluate(js_code, payload)
    
    # --- 详细响应日志 ---
    status = result.get("status")
    if status == 200:
        json_data = result.get("json", {})
        code = json_data.get("code")
        msg = json_data.get("message") or json_data.get("msg")
        
        if code == 0:
            logger.info(f"✅ [{tag}] Success (Code: 0)")
            # 尝试打印数据条数概览
            data_content = json_data.get("data", {})
            if isinstance(data_content, dict):
                keys = list(data_content.keys())
                logger.info(f"   📄 [{tag}] Data Keys: {keys}")
                if "product_operation" in data_content:
                    count = len(data_content["product_operation"])
                    logger.info(f"   📦 [{tag}] Found {count} products")
                if "poi_operation" in data_content:
                    count = len(data_content["poi_operation"])
                    logger.info(f"   Store Count: {count}")
                if "pois" in data_content:
                    count = len(data_content["pois"])
                    logger.info(f"   Store Relations: {count}")
        else:
            logger.error(f"❌ [{tag}] API Error: Code={code}, Msg={msg}")
            log_json_preview(f"   [{tag}] Response Body", json_data)
    else:
        logger.error(f"❌ [{tag}] HTTP Fail: Status={status}, Error={result.get('error')}")
    
    return result

# ==========================================================
# 4. 接口路由
# ==========================================================

# --- 1. 获取门店列表 (Competitors) ---
@app.post("/topk")
async def get_topk_data(payload: GeneralPayload):
    """获取竞品列表"""
    logger.info(f"📥 [Endpoint] /topk - Radius: {payload.locsight_fence.radius}")
    
    if not browser_context: raise HTTPException(503, "Service not ready")
    page = await browser_context.new_page()
    
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        url = f"{URL_TOPK_STORES}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        clean_data = build_payload(payload, etype=1, specific_ids=None)
        
        result = await fetch_api_in_browser(page, url, clean_data, tag="TopK-Stores")
        
        if result.get("status") == 200:
            return result.get("json")
        else:
            raise HTTPException(500, f"Upstream Error: {result}")
    finally:
        await page.close()

# --- 2. 获取用户画像 ---
@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    logger.info(f"📥 [Endpoint] /portrait - POI: {payload.awe_poi_id}")
    
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

# --- 3. 获取商品套餐 (智能组装 - 串行逻辑) ---
@app.post("/products")
async def get_products_data(payload: GeneralPayload):
    """
    Step 1: 调 /topk/v2 获取商品列表
    Step 2: 调 /topk/pois/v2 (带IDs) 获取关联
    Step 3: 组装
    """
    logger.info(f"📥 [Endpoint] /products - Radius: {payload.locsight_fence.radius}")
    
    if not browser_context: raise HTTPException(503, "Service not ready")
    page = await browser_context.new_page()
    
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        # === Step 1: 获取范围内热门商品 ===
        url_step1 = f"{URL_TOPK_PRODUCTS}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        payload_step1 = build_payload(payload, etype=2, specific_ids=None)
        
        res1 = await fetch_api_in_browser(page, url_step1, payload_step1, tag="Step1-GetProducts")
        
        products_list = res1.get("json", {}).get("data", {}).get("product_operation", [])
        
        if not products_list:
            logger.warning("   ⚠️ Step 1 返回空商品列表，流程提前结束")
            return {"code": 0, "message": "success (no products)", "data": []}

        # 打印部分商品信息以供调试
        logger.info(f"   ✅ Step 1: 成功获取 {len(products_list)} 个商品")
        if len(products_list) > 0:
            sample_prod = products_list[0]
            logger.info(f"      示例商品: {sample_prod.get('product_name')} (ID: {sample_prod.get('product_id')})")

        # 提取所有商品 ID
        product_ids = [str(p['product_id']) for p in products_list]

        # === Step 2: 获取这些商品所属的门店 ===
        url_step2 = f"{URL_TOPK_STORES}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        # 这里的关键：entity_ids 必须传回去
        payload_step2 = build_payload(payload, etype=2, specific_ids=product_ids)
        
        res2 = await fetch_api_in_browser(page, url_step2, payload_step2, tag="Step2-GetRelations")
        
        stores_list = res2.get("json", {}).get("data", {}).get("pois", [])
        logger.info(f"   ✅ Step 2: 成功获取 {len(stores_list)} 个门店关联信息")

        # === Step 3: 数据组装 (Join) ===
        product_map = {str(p['product_id']): p for p in products_list}
        
        final_result = []
        for store in stores_list:
            store_name = store.get('name')
            store_id = store.get('awe_poi_id')
            
            store_obj = {
                "awe_poi_id": store_id,
                "name": store_name,
                "product_details": []
            }
            
            related_ids = store.get('related_entity_ids', [])
            
            for pid in related_ids:
                pid_str = str(pid)
                if pid_str in product_map:
                    store_obj['product_details'].append(product_map[pid_str])
            
            if len(store_obj['product_details']) > 0:
                final_result.append(store_obj)

        logger.info(f"🎉 [Endpoint] /products 完成: 组装了 {len(final_result)} 个门店的套餐数据")
        log_json_preview("   最终返回数据示例", final_result, max_len=300)
        
        return {
            "code": 0,
            "message": "success",
            "data": final_result
        }

    except Exception as e:
        logger.error(f"❌ [Endpoint] /products 崩溃: {str(e)}")
        return {"code": -1, "message": str(e), "data": []}
    finally:
        await page.close()

@app.get("/")
def read_root():
    return {"status": "ok", "routes": ["/topk", "/products", "/portrait"]}

if __name__ == "__main__":
    import uvicorn
    # 获取环境变量端口，适配 Cloud Run
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
