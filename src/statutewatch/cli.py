"""命令行入口：默认跑旧流水线（兼容），--regulations 跑法规批次流水线。"""
import json
import sys

from . import pipeline


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--regulations" in argv:
        result = pipeline.run_regulations()
    else:
        result = pipeline.run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
