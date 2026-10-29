"""Static, local, dependency-free test: no installation, render or network."""
import json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
read=lambda f:json.loads((root/f).read_text(encoding="utf-8"))
a,s,p=read("data/sample.json"),read("data/style.json"),read("package.json")
assert a["language"]=="ar" and 15<=a["durationSeconds"]<=300
assert a["scenes"][0]["startSeconds"]==0 and len(a["scenes"])<=6
assert [x["startSeconds"] for x in a["scenes"]]==sorted({x["startSeconds"] for x in a["scenes"]})
assert a["scenes"][-1]["startSeconds"]<a["durationSeconds"]
assert set(x["kind"] for x in a["scenes"])<={"library","courtyard","harbor"}
assert s["rtl"] and not s["remoteFonts"] and not s["flashes"] and not s["hardCuts"]
assert s["silent"] and (s["fps"],s["width"],s["height"])==(24,1920,1080)
assert p["private"] and "deploy" not in p["scripts"] and "publish" not in p["scripts"]
assert "@remotion/lambda" not in p["dependencies"]
print("CONTRACT_STATIC_CHECK=PASS; VISUAL_QA=NOT_RUN; RENDER=NOT_RUN")
