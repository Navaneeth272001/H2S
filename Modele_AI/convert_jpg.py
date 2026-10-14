import os
from PIL import Image

for f in os.listdir('trained_models'):
    if f.endswith('.png'):
        path = os.path.join('trained_models', f)
        img = Image.open(path).convert('RGB')
        img.save(path.replace('.png', '.jpg'), 'JPEG')
print("Converted to JPG")
