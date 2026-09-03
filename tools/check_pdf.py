import sys
sys.path.insert(0, r"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\Lib\site-packages")
from pypdf import PdfReader
r = PdfReader(r"C:\Users\Administrator\Desktop\简历-麦当.pdf")
print(f"Pages: {len(r.pages)}")
for i, p in enumerate(r.pages):
    t = p.extract_text()
    print(f"--- Page {i+1} ({len(t)} chars) ---")
    print(t[:500])
