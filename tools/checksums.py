"""Печатает SHA-256 файлов в формате sha256sum: «<хэш>  <имя>»."""
import hashlib
import sys
from pathlib import Path

for name in sys.argv[1:]:
    path = Path(name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"{digest}  {path.name}")
