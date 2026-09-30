import hashlib
import json

path = "artifacts/medium_out.md"
data = open(path, "rb").read()
lines = data.decode("utf-8").splitlines()
info = {
    "path": path,
    "sha256": hashlib.sha256(data).hexdigest(),
    "bytes": len(data),
    "rows": len(lines),
    "preview_first_5_lines": lines[:5],
}
print(json.dumps(info, ensure_ascii=False, indent=2))
with open("artifacts/_medium_manifest_input.json", "w", encoding="utf-8") as fh:
    json.dump(info, fh, ensure_ascii=False, indent=2)
