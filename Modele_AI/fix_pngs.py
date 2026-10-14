import os
from PIL import Image

models_dir = 'trained_models'
for f in os.listdir(models_dir):
    if f.endswith('.png'):
        path = os.path.join(models_dir, f)
        img = Image.open(path)
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            print(f"Fixing transparency in {f}")
            alpha = img.convert('RGBA').split()[-1]
            bg = Image.new("RGBA", img.size, (255,255,255,255))
            bg.paste(img, mask=alpha)
            bg.convert('RGB').save(path)
        else:
            # Just to be sure, re-save as RGB with white bg
            img = img.convert('RGBA')
            bg = Image.new("RGBA", img.size, (255,255,255,255))
            bg.paste(img, mask=img.split()[3] if len(img.split())==4 else None)
            bg.convert('RGB').save(path)
print("Done")
