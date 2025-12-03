import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Any
from playwright.async_api import async_playwright, BrowserContext

# 日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger("LBS")

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
# 2. 生命周期
# ==========================================================

playwright_instance = None
browser_context: Optional[BrowserContext] = None
AUTH_FILE = "auth.json"

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser_context
    logger.info("🚀 启动中...")
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(headless=True, args=['--no-sandbox'])
    try:
        browser_context = await browser.new_context(storage_state=AUTH_FILE)
        logger.info("✅ Cookie 加载成功")
    except:
        browser_context = await browser.new_context()
    yield
    if browser_context: await browser_context.close()
    if playwright_instance: await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

# ==========================================================
# 3. 核心工具
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'
URL_TOPK_PRODUCTS = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
URL_TOPK_STORES   = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/pois/v2'
URL_PORTRAIT      = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'

def build_strict_payload(origin: GeneralPayload, etype: int, specific_ids: List[str] = None):
    """
    构造极其严格的 Payload。
    如果字段没值，绝对不出现在字典里。
    """
    fence_data = {
        "poi_id": origin.locsight_fence.poi_id,
        "radius": origin.locsight_fence.radius
    }
    
    # ⚠️ 关键修改：只有经纬度真的存在且不为0时，才加入字典
    # 否则直接不传这两个 key，强迫 API 使用 poi_id 进行搜索
    if origin.locsight_fence.center_lng and origin.locsight_fence.center_lat:
        fence_data["center_lng"] = origin.locsight_fence.center_lng
        fence_data["center_lat"] = origin.locsight_fence.center_lat

    data = {
        "entity_type": etype,
        "locsight_fence": fence_data,
        "locsight_time": origin.locsight_time.dict(),
        "awe_type_code": origin.awe_type_code.dict()
    }
    
    if specific_ids is not None:
        data["entity_ids"] = specific_ids

    return data

async def fetch_api(page, url, payload, tag="API"):
    # 调试日志：打印实际发出的 JSON 字符串
    logger.info(f"⚡ [{tag}] Request -> {url.split('?')[0]}")
    logger.info(f"📦 [{tag}] Payload -> {json.dumps(payload, ensure_ascii=False)}")
    
    js_code = f"""
        async (payload) => {{
            try {{
                const response = await fetch('{url}', {{
                    method: 'POST',
                    headers: {{ 'content-type': 'application/json', 'user': '{USER_ID}' }},
                    body: JSON.stringify(payload)
                }});
                const text = await response.text();
                try {{ return {{ s: response.status, j: JSON.parse(text) }}; }}
                catch (e) {{ return {{ s: response.status, e: 'JSON Error' }}; }}
            }} catch(e) {{ return {{ s: -1, e: e.toString() }}; }}
        }}
    """
    return await page.evaluate(js_code, payload)

# ==========================================================
# 4. 路由
# ==========================================================

# 1. 门店列表
@app.post("/topk")
async def get_topk_data(payload: GeneralPayload):
    if not browser_context: raise HTTPException(503)
    page = await browser_context.new_page()
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        url = f"{URL_TOPK_STORES}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        # 强制不带 IDs，且严格检查经纬度
        data = build_strict_payload(payload, 1, specific_ids=None)
        
        res = await fetch_api(page, url, data, "TopK")
        if res['s'] == 200: return res['j']
        raise HTTPException(500, f"Error: {res}")
    finally:
        await page.close()

# 2. 画像
@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    if not browser_context: raise HTTPException(503)
    page = await browser_context.new_page()
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass
        url = f"{URL_PORTRAIT}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        # 画像 Payload 也要清洗一下经纬度
        data = payload.dict()
        if not data['locsight_fence'].get('center_lng'):
            del data['locsight_fence']['center_lng']
            del data['locsight_fence']['center_lat']
            
        res = await fetch_api(page, url, data, "Portrait")
        if res['s'] == 200: return res['j']
        raise HTTPException(500, f"Error: {res}")
    finally:
        await page.close()

# 3. 商品套餐 (串行组装)
@app.post("/products")
async def get_products_data(payload: GeneralPayload):
    if not browser_context: raise HTTPException(503)
    page = await browser_context.new_page()
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        # Step 1: 查商品 (无 ID)
        url1 = f"{URL_TOPK_PRODUCTS}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        data1 = build_strict_payload(payload, 2, None)
        res1 = await fetch_api(page, url1, data1, "Step1")
        products = res1.get('j', {}).get('data', {}).get('product_operation', [])
        
        if not products: return {"code": 0, "data": []}

        # Step 2: 查关联 (有 ID)
        pids = [str(p['product_id']) for p in products]
        url2 = f"{URL_TOPK_STORES}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        data2 = build_strict_payload(payload, 2, pids)
        res2 = await fetch_api(page, url2, data2, "Step2")
        stores = res2.get('j', {}).get('data', {}).get('pois', [])

        # Step 3: 组装
        pmap = {str(p['product_id']): p for p in products}
        result = []
        for s in stores:
            obj = {"awe_poi_id": s.get('awe_poi_id'), "name": s.get('name'), "product_details": []}
            for pid in s.get('related_entity_ids', []):
                if str(pid) in pmap: obj['product_details'].append(pmap[str(pid)])
            if obj['product_details']: result.append(obj)

        logger.info(f"🎉 组装完成: {len(result)} 条数据")
        return {"code": 0, "message": "success", "data": result}
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return {"code": -1, "msg": str(e)}
    finally:
        await page.close()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
