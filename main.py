import asyncio
import json
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Any
from playwright.async_api import async_playwright, BrowserContext

# ==========================================================
# 0. 日志配置
# ==========================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LBS_Proxy")

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

# 两个核心 API 地址
URL_TOPK_PRODUCTS = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
URL_TOPK_STORES   = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/pois/v2'
URL_PORTRAIT      = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'

def build_payload(origin: GeneralPayload, etype: int, specific_ids: List[str] = None):
    """
    构造请求体。
    - etype: 强制指定 entity_type
    - specific_ids: 如果传入列表，则填充 entity_ids；否则该字段不发送。
    """
    data = {
        "entity_type": etype,
        "locsight_fence": {
            "poi_id": origin.locsight_fence.poi_id,
            "radius": origin.locsight_fence.radius
        },
        "locsight_time": origin.locsight_time.dict(),
        "awe_type_code": origin.awe_type_code.dict()
    }
    
    # 只有当明确传入 ID 列表时，才添加该字段
    if specific_ids is not None:
        data["entity_ids"] = specific_ids
        
    # 可选：补充经纬度
    if origin.locsight_fence.center_lng:
        data["locsight_fence"]["center_lng"] = origin.locsight_fence.center_lng
    if origin.locsight_fence.center_lat:
        data["locsight_fence"]["center_lat"] = origin.locsight_fence.center_lat

    return data

async def fetch_api_in_browser(page, url, payload, tag="API"):
    """执行浏览器 fetch"""
    logger.info(f"⚡ [{tag}] POST -> {url.split('?')[0]}")
    
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
                    return {{ status: response.status, error: 'JSON Parse Error', text: text.substring(0, 100) }};
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
    获取竞品列表。
    URL: .../topk/pois/v2
    Payload: 不带 entity_ids
    """
    if not browser_context: raise HTTPException(503, "Service not ready")
    page = await browser_context.new_page()
    
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        url = f"{URL_TOPK_STORES}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        # 强制不带 IDs
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
    两步走策略：
    Step 1: 调 /topk/v2 获取商品列表 (得到 product_id)。
    Step 2: 调 /topk/pois/v2 带上 entity_ids，获取门店关联关系。
    Step 3: 组装返回。
    """
    logger.info(f"📥 [Request] /products (Radius: {payload.locsight_fence.radius})")
    
    if not browser_context: raise HTTPException(503, "Service not ready")
    page = await browser_context.new_page()
    
    try:
        # 预导航
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        # === Step 1: 获取范围内热门商品 ===
        # URL: .../topk/v2
        # Payload: 不带 entity_ids, entity_type=2
        url_step1 = f"{URL_TOPK_PRODUCTS}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        payload_step1 = build_payload(payload, etype=2, specific_ids=None)
        
        res1 = await fetch_api_in_browser(page, url_step1, payload_step1, tag="Step1-GetProducts")
        
        products_list = res1.get("json", {}).get("data", {}).get("product_operation", [])
        logger.info(f"   ↳ Step 1 找到 {len(products_list)} 个商品")

        if not products_list:
            return {"code": 0, "message": "success (no products)", "data": []}

        # 提取所有商品 ID
        product_ids = [str(p['product_id']) for p in products_list]

        # === Step 2: 获取这些商品所属的门店 ===
        # URL: .../topk/pois/v2
        # Payload: 带上 entity_ids (就是刚才拿到的商品ID), entity_type=2
        url_step2 = f"{URL_TOPK_STORES}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        payload_step2 = build_payload(payload, etype=2, specific_ids=product_ids)
        
        res2 = await fetch_api_in_browser(page, url_step2, payload_step2, tag="Step2-GetRelations")
        
        stores_list = res2.get("json", {}).get("data", {}).get("pois", [])
        logger.info(f"   ↳ Step 2 找到 {len(stores_list)} 个关联门店")

        # === Step 3: 数据组装 (Join) ===
        # 建立 ID -> 商品详情 的映射
        product_map = {str(p['product_id']): p for p in products_list}
        
        final_result = []
        for store in stores_list:
            store_obj = {
                "awe_poi_id": store.get('awe_poi_id'),
                "name": store.get('name'), # 拿到店名
                "product_details": []
            }
            
            # 这里的 related_entity_ids 就是商品 ID
            related_ids = store.get('related_entity_ids', [])
            
            for pid in related_ids:
                pid_str = str(pid)
                if pid_str in product_map:
                    store_obj['product_details'].append(product_map[pid_str])
            
            # 仅返回包含有效商品的门店
            if len(store_obj['product_details']) > 0:
                final_result.append(store_obj)

        logger.info(f"✅ [Finish] 组装完成，返回 {len(final_result)} 个门店数据")
        
        return {
            "code": 0,
            "message": "success",
            "data": final_result
        }

    except Exception as e:
        logger.error(f"❌ [Error] /products 流程失败: {e}")
        # 返回空数据防止前端崩
        return {"code": -1, "message": str(e), "data": []}
    finally:
        await page.close()

@app.get("/")
def read_root():
    return {"status": "ok", "routes": ["/topk", "/products", "/portrait"]}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
