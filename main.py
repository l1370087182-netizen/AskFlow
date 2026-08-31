import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent/"src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
    
def create_app():
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from controller.konwledge_controller import router as knowledge_router
    from controller.embedding_controller import router as embedding_router
    from controller.retrieval_controller import router as retrieval_router
    from controller.chat_controller import router as chat_router
    from controller.card_controller import router as card_router
    from controller.jd_controller import router as jd_router
    from controller.evaluate_controller import router as evaluate_router
    from controller.interview_controller import router as interview_router

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

    app.include_router(knowledge_router)
    app.include_router(embedding_router)
    app.include_router(retrieval_router)
    app.include_router(chat_router)
    app.include_router(card_router)
    app.include_router(jd_router)
    app.include_router(evaluate_router)
    app.include_router(interview_router)
    return app


def main():
    import uvicorn
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=4399)

if __name__ == "__main__":
    main()
