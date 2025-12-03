import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from playwright.async_api import async_playwright, BrowserContext

# ==========================================================
# 1. Pydantic 模型定义
# ==========================================================

class Fence(BaseModel):
    poi_id: str
    radius: int
    center_lng: Optional[float] = None
    center_lat: Optional[float] = None

class Time(BaseModel):
    raw_text: str  # e.g., "2024-10"

class AweTypeCode(BaseModel):
    code: str
    level: int

# --- /portrait 接口模型 ---
class PortraitPayload(BaseModel):
    awe_poi_id: str
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode
    entity_type: int = 1

# --- /topk 接口模型 (门店列表) ---
class TopkPayload(BaseModel):
    entity_type: int = 1
    entity_ids: List[str] = [] # 必填，但可以是空数组
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

# --- /products 接口模型 (套餐商品) ---
class ProductPayload(BaseModel):
    entity_type: int = 2       # 商品接口必须是 2
    entity_ids: List[str] = [] 
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

# ==========================================================
# 2. 全局变量和 Playwright 生命周期管理
# ==========================================================

playwright_instance = None
browser_context: Optional[BrowserContext] = None
AUTH_FILE = "auth.json"

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser_context
    print("🚀 服务启动中，正在初始化 Playwright...")
    
    playwright_instance = await async_playwright().start()
    
    # 在 Docker/Serverless 环境中，sandbox 参数通常是必须的
    browser_args = ['--no-sandbox', '--disable-setuid-sandbox']
    
    browser = await playwright_instance.chromium.launch(
        headless=True, 
        args=browser_args
    )
    
    try:
        print(f"📂 正在加载 Cookie 文件: {AUTH_FILE}")
        browser_context = await browser.new_context(storage_state=AUTH_FILE)
        print("✅ 浏览器上下文加载成功")
    except Exception as e:
        print(f"⚠️ 加载 auth.json 失败 (可能是文件不存在或格式错误): {e}")
        print("⚠️ 将使用空上下文启动 (可能导致需要登录的接口失败)")
        browser_context = await browser.new_context()

    yield
    
    print("🛑 服务关闭中，正在释放资源...")
    if browser_context:
        await browser_context.close()
    if playwright_instance:
        await playwright_instance.stop()
    print("✅ Playwright 已关闭")

app = FastAPI(lifespan=lifespan)

# ==========================================================
# 3. 核心签名与请求函数
# ==========================================================

async def get_signed_response(target_url: str, payload: dict, user_id: str):
    if not browser_context:
        raise HTTPException(status_code=503, detail="Playwright service not ready")

    response_future = asyncio.Future()
    page = await browser_context.new_page()

    # --- 调试日志监听 ---
    # 这能帮你看到浏览器内部是否报 403, CORS 或 JS 错误
    page.on("console", lambda msg: print(f"[Browser Console] {msg.text}"))
    page.on("pageerror", lambda exc: print(f"[Browser Error] {exc}"))

    # --- 响应拦截器 ---
    async def handle_response(response):
        # 匹配 URL (忽略 query 参数差异)
        req_url = target_url.split('?')[0]
        
        if req_url in response.url and response.request.method == "POST":
            print(f"🔍 捕获 API 响应: {response.status} | {response.url[:60]}...")
            
            if not response_future.done():
                if response.ok:
                    try:
                        json_data = await response.json()
                        response_future.set_result(json_data)
                    except Exception as e:
                        print(f"❌ JSON 解析失败: {e}")
                        response_future.set_exception(e)
                else:
                    # 如果 API 报错 (如 403/500)，尝试读取错误文本
                    try:
                        err_text = await response.text()
                        print(f"❌ API 请求失败 ({response.status}): {err_text[:200]}")
                        response_future.set_exception(Exception(f"Upstream API Error {response.status}: {err_text}"))
                    except:
                        response_future.set_exception(Exception(f"Upstream API Error {response.status}"))
    
    page.on("response", handle_response)

    try:
        # --- 关键步骤: 导航到目标域 ---
        # 这确保了 Origin/Referer 正确，且 Cookie 能被浏览器附带
        entry_url = "https://lbs-locsight.bytedance.com/locsight/result"
        print("🧭 正在导航到宿主页面以激活 Cookie...")
        try:
            # timeout 设短点，只要域名变了就行，不需要等全加载完
            await page.goto(entry_url, timeout=15000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"⚠️ 导航超时或部分加载 (通常可忽略): {e}")

        # --- JS 注入发送请求 ---
        js_code = f"""
            async (payload) => {{
                console.log("🚀 [In-Browser] 开始发送 XHR 请求...");
                const url = '{target_url}';
                const body = JSON.stringify(payload);
                
                const xhr = new XMLHttpRequest();
                xhr.open('POST', url, true);
                xhr.setRequestHeader('content-type', 'application/json');
                xhr.setRequestHeader('user', '{user_id}');
                
                xhr.onload = () => console.log('✅ [In-Browser] XHR 完成, Status: ' + xhr.status);
                xhr.onerror = () => console.error('❌ [In-Browser] XHR 网络错误');
                
                xhr.send(body);
            }}
        """
        
        print(f"⚡ 正在注入请求到: {target_url.split('?')[0]}")
        await page.evaluate(js_code, payload)
        
        # 等待 Future 结果
        result = await asyncio.wait_for(response_future, timeout=25.0)
        return result

    except asyncio.TimeoutError:
        print("❌ 请求超时: 25秒内未收到目标 API 的响应")
        raise HTTPException(status_code=504, detail="Request timed out. Upstream API did not respond in time.")
    except Exception as e:
        print(f"❌ 系统内部错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await page.close()

# ==========================================================
# 4. API 路由定义
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'

@app.get("/")
def read_root():
    return {"status": "running", "service": "Douyin LBS Proxy"}

# --- 1. 画像接口 ---
@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    print(f"📥 收到 /portrait 请求")
    return await get_signed_response(target_url, payload.dict(), USER_ID)

# --- 2. 竞对门店列表接口 ---
@app.post("/topk")
async def get_topk_data(payload: TopkPayload):
    # 注意 URL 包含 /pois/v2
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/pois/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    print(f"📥 收到 /topk 请求 (门店列表), radius: {payload.locsight_fence.radius}")
    return await get_signed_response(target_url, payload.dict(), USER_ID)

# --- 3. 商品套餐接口 (新增) ---
@app.post("/products")
async def get_products_data(payload: ProductPayload):
    # 注意 URL 是 /topk/v2 (没有 pois)
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    # 强制修正 entity_type 为 2
    data = payload.dict()
    data['entity_type'] = 2
    
    print(f"📥 收到 /products 请求 (套餐商品), radius: {payload.locsight_fence.radius}")
    return await get_signed_response(target_url, data, USER_ID)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
