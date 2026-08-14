from fastapi import FastAPI, HTTPException

from kis_api import get_current_price

app = FastAPI(title="KIS Open API LAB")


@app.get("/")
def root():
    return {"message": "KIS Open API LAB 정상 실행"}


@app.get("/api/price/{stock_code}")
def current_price(stock_code: str):
    try:
        result = get_current_price(stock_code)
        output = result.get("output", {})

        return {
            "stock_code": stock_code,
            "current_price": output.get("stck_prpr"),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))