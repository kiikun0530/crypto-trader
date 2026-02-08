"""model.tar.gz パッケージスクリプト (Terraform local-exec 用)"""
import tarfile
import sys
import os

output_path = sys.argv[1]
source_dir = os.path.join(os.path.dirname(__file__), "chronos-tiny")

os.makedirs(os.path.dirname(output_path), exist_ok=True)

with tarfile.open(output_path, "w:gz") as tar:
    tar.add(os.path.join(source_dir, "code"), arcname="code")

print(f"Created {output_path}")
