import os

import uvicorn

if __name__ == "__main__":
    # ServiceMaker/Container Manager usually provides a PORT env var
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    uvicorn.run(
        "arango_cypher.service:app",
        host=host,
        port=port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
