import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent/"src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
    
def create_app():
    from fastapi import Depends, FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from auth.deps import get_current_user
    from controller.konwledge_controller import router as knowledge_router
    from controller.embedding_controller import router as embedding_router
    from controller.retrieval_controller import router as retrieval_router
    from controller.chat_controller import router as chat_router
    from controller.card_controller import router as card_router
    from controller.jd_controller import router as jd_router
    from controller.evaluate_controller import router as evaluate_router
    from controller.interview_controller import router as interview_router
    from controller.auth_controller import router as auth_router
    from controller.user_controller import router as user_router

    app = FastAPI(title="智能问答系统（学习版）")

    # 前端静态站在 10001 端口（10000 与百度网盘服务冲突，预留兼容），放开跨域
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://.*:1000[01]",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def read_root():
        return {"message": "Hello from rag!"}

    # 鉴权：仅注册/登录/验证码接口免登录，其余全部要求 Bearer token
    app.include_router(auth_router)

    _auth = [Depends(get_current_user)]
    for r in (
        knowledge_router,
        embedding_router,
        retrieval_router,
        chat_router,
        card_router,
        jd_router,
        evaluate_router,
        interview_router,
        user_router,
    ):
        app.include_router(r, dependencies=_auth)

    # 个人知识库：幂等拉起爬取任务后台消费线程（模块级标记，daemon=True）。
    # 注意：勿在 uvicorn --reload 下使用爬取功能（多进程会重复消费）
    from service.knowledge_service import start_crawl_worker
    start_crawl_worker()
    return app


def main():
    import uvicorn
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=4399)

if __name__ == "__main__":
    main()
