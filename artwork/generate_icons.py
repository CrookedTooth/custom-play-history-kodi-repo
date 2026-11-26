"""Original editable geometric artwork for the Kunsemiller Kodi packages."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import math

out = Path(__file__).resolve().parent
S = 2048
def canvas(color):
    im = Image.new('RGBA', (S,S), (0,0,0,0))
    ImageDraw.Draw(im).rounded_rectangle((48,48,2000,2000), radius=350, fill=color)
    return im
def save(im, name):
    im.resize((512,512),Image.Resampling.LANCZOS).save(out/name, optimize=True)

im=canvas('#172B3B'); d=ImageDraw.Draw(im)
d.ellipse((365,240,1683,1558),fill='#101820',outline='#385467',width=14)
for radius in (550,480,410,340,270):
    d.ellipse((1024-radius,899-radius,1024+radius,899+radius),outline='#385467',width=12)
d.ellipse((862,737,1186,1061), fill='#64C9ED')
d.ellipse((994,869,1054,929), fill='#101820')
for x,y in ((500,1660),(850,1530),(1200,1390)):
    d.rounded_rectangle((x,y,x+250,1845),radius=55,fill='#64C9ED')
save(im,'custom-play-history.png')

im=canvas('#172B3B'); d=ImageDraw.Draw(im)
d.line([(1024,250),(1798,1024),(1024,1798),(250,1024),(1024,250)], fill='#64C9ED',width=48,joint='curve')
font=ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf',1000)
box=d.textbbox((0,0),'K',font=font); d.text(((S-(box[2]-box[0]))/2-box[0],(S-(box[3]-box[1]))/2-box[1]-10),'K',font=font,fill='white')
save(im,'kunsemiller-repository.png')

im=canvas('#303840'); d=ImageDraw.Draw(im)
cx=cy=1024
points=[]
for j in range(96):
    a=2*math.pi*j/96-math.pi/2
    r=695 if j%8 in (2,3,4,5) else 560
    points.append((cx+r*math.cos(a),cy+r*math.sin(a)))
d.polygon(points,fill='#C4CDD2')
d.ellipse((620,620,1428,1428),fill='#C4CDD2')
d.ellipse((835,835,1213,1213),fill='#303840')
save(im,'music-history-dependency.png')
save(im,'silvo-integration-dependency.png')
