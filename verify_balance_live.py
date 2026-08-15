import json

from kis_api import get_account_balance


def main():
    data = get_account_balance()
    print(json.dumps({
        "rt_cd": data.get("rt_cd"),
        "msg_cd": data.get("msg_cd"),
        "msg1": data.get("msg1"),
        "has_output1": "output1" in data,
        "has_output2": "output2" in data,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
