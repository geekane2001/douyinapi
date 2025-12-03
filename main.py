import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from playwright.async_api import async_playwright, BrowserContext

# ==========================================================
# 1. Pydantic 模型：定义请求体的数据结构，用于自动验证
# ==========================================================

class Fence(BaseModel):
    poi_id: str
    radius: int

class Time(BaseModel):
    raw_text: str # e.g., "2025-10"

class AweTypeCode(BaseModel):
    code: str
    level: int

# --- Payload for the 'portrait' API ---
class PortraitPayload(BaseModel):
    awe_poi_id: str
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode
    entity_type: int = 1

# --- Payload for the 'topk' API ---
# 注意：根据你的JS代码，第二个接口是 /topk/v2，它可能需要不同的参数
# 这里根据第一个接口的结构创建了一个合理的模型，你可能需要根据实际情况调整
class TopkPayload(BaseModel):
    entity_type: int = 2
    entity_ids: List[str]
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

# ==========================================================
# 2. 全局变量和 Playwright 生命周期管理
# ==========================================================

# 存储 Playwright 实例的全局变量
playwright_instance = None
browser_context: Optional[BrowserContext] = None
AUTH_FILE = "auth.json"

# 使用 FastAPI 的 lifespan 管理器，在服务启动时开启浏览器，在关闭时关闭
@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser_context
    print("服务启动中，正在初始化 Playwright...")
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(headless=True) # 在 Docker 中必须是 headless
    browser_context = await browser.new_context(storage_state=AUTH_FILE)
    print("Playwright 初始化完成，浏览器已准备就绪。")
    yield
    print("服务关闭中，正在关闭 Playwright...")
    await browser_context.close()
    await playwright_instance.stop()
    print("Playwright 已成功关闭。")

app = FastAPI(lifespan=lifespan)

# ==========================================================
# 3. 核心签名函数 (修改版)
# ==========================================================

async def get_signed_response(target_url: str, payload: dict, user_id: str):
    if not browser_context:
        raise HTTPException(status_code=503, detail="Playwright service not ready")

    response_future = asyncio.Future()
    page = await browser_context.new_page()

    # --- 1. 增加浏览器调试日志监听 ---
    page.on("console", lambda msg: print(f"[Browser Console] {msg.text}"))
    page.on("pageerror", lambda exc: print(f"[Browser Error] {exc}"))
    
    # 监听请求失败的情况 (如网络被墙、DNS错误)
    page.on("requestfailed", lambda request: print(f"[Request Failed] {request.url} - {request.failure}"))

    async def handle_response(response):
        # 打印所有相关的响应 URL，用于调试
        if "lbs-locsight.bytedance.com" in response.url:
            print(f"检测到流量: {response.status} | {response.url[:60]}...")

        # 稍微放宽匹配条件，防止 query 参数顺序不同导致匹配失败
        # 只要 url 包含 API 路径且是 POST 即可
        api_path = target_url.split("?")[0] 
        
        if api_path in response.url and response.request.method == "POST":
            print(f"✅ 成功捕获目标API响应: {response.status}")
            if not response_future.done():
                try:
                    # 如果状态码不是 200，也尝试读取 body 以查看错误信息
                    json_data = await response.json()
                    response_future.set_result(json_data)
                except Exception as e:
                    print(f"❌ 解析 JSON 失败: {e}")
                    response_future.set_exception(e)
    
    page.on("response", handle_response)

    try:
        # --- 2. 关键修复：先导航到目标域名 ---
        # 这确保了 Cookie 生效，并且 Origin/Referer 正确
        print("正在导航到目标域名以初始化上下文...")
        try:
            # 访问一个该域名的轻量页面，或者直接访问首页
            # timeout 设置短一点，我们不关心页面是否完全加载，只要域名对就行
            await page.goto("https://lbs-locsight.bytedance.com/", timeout=10000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"导航警告 (通常可忽略): {e}")

        # --- 3. 执行请求 ---
        js_function = f"""
            async (payload) => {{
                console.log("开始在浏览器内发送 XHR 请求...");
                const url = '{target_url}';
                const body = JSON.stringify(payload);
                
                try {{
                    const response = await fetch(url, {{
                        method: 'POST',
                        headers: {{
                            'content-type': 'application/json',
                            'user': '{user_id}'
                        }},
                        body: body
                    }});
                    console.log("Fetch 请求完成，状态码: " + response.status);
                    return "Request Sent";
                }} catch (e) {{
                    console.error("Fetch 请求发生错误: " + e);
                    throw e;
                }}
            }}
        """
        
        # 使用 fetch 替代 XMLHttpRequest (代码更现代，且容易调试)，Playwright 同样能捕获
        print(f"正在注入 JS 请求: {target_url[:50]}...")
        await page.evaluate(js_function, payload)
        
        # 等待结果
        result = await asyncio.wait_for(response_future, timeout=25.0)
        return result

    except asyncio.TimeoutError:
        print("❌ 请求超时 - 未能捕获到匹配的响应包")
        # 抛出超时异常时，查看控制台是否有相关错误日志
        raise HTTPException(status_code=504, detail="Request timed out waiting for upstream API response. Check server logs.")
    except Exception as e:
        print(f"❌ 内部错误: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
    finally:
        await page.close()

# ==========================================================
# 4. FastAPI 接口端点
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'

@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    """
    代理并签名 /arrive/portrait/v2 接口
    """
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    print(f"收到 /portrait 请求, poi_id: {payload.awe_poi_id}")
    response_data = await get_signed_response(target_url, payload.dict(), USER_ID)
    return response_data

@app.post("/topk")
async def get_topk_data(payload: TopkPayload):
    """
    代理并签名 /topk/v2 接口 (注意：原始脚本是 /topk/pois/v2)
    """
    # 请确认这个URL是否正确，你的JS代码中是 /topk/v2
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    print(f"收到 /topk 请求, poi_id: {payload.locsight_fence.poi_id}")
    response_data = await get_signed_response(target_url, payload.dict(), USER_ID)
    return response_data

@app.get("/")
def read_root():
    return {"status": "Signature service is running"}
