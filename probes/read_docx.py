
import zipfile, re, sys, pathlib
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
src = r"C:\Users\Redmi\.dsh\attachments\v1\files\85\8586e547ebe3c5e50b7ed6a529b8223881aa9a57ae598d9833a9437c9341d8e2\Доп задание на 5 баллов.docx"
dst = pathlib.Path(r"C:\specialinay_papka\probes\docx.txt")
with zipfile.ZipFile(src) as z:
    xml = z.read("word/document.xml").decode("utf-8", "replace")
xml = re.sub(r"</w:p>", "\n", xml)
xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
text = re.sub(r"<[^>]+>", "", xml)
text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
dst.write_text(text, encoding="utf-8")
print("chars:", len(text))
print(text[:1200])
