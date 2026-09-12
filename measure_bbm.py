from pathlib import Path
import re, math
layouts={666:(18,6,108,5,1),888:(24,8,108,7,1),1221:(22,11,109,10,2),1665:(15,15,110,14,2),1887:(17,17,110,16,2),2109:(19,19,110,18,2),2775:(25,25,110,24,2)}
def inspect(p):
    raw=p.read_bytes(); layout=layouts.get(len(raw))
    if layout is None and len(raw)%111==0:
        slot=len(raw)//111; ws=[raw[slot+i*slot] for i in range(110)]; bpc=0
        for w in ws:
            if w: bpc=w if not bpc else math.gcd(bpc,w)
        if bpc in (1,2,3,4) and all(w<=slot-1 for w in ws): layout=(slot,slot,110,slot-1,bpc)
    if layout is None: raise ValueError('unsupported '+str(p))
    header,slot,count,maxw,bpc=layout
    m=re.search(r'x(\d+)',p.stem,re.I)
    if m: height=int(m.group(1))
    elif p.stem.upper()=='F-DIGITAL': height=8
    else:
        m=re.search(r'-(\d+)(?:\s|$)',p.stem)
        if m: height=int(m.group(1))
        elif len(raw)%111==0: height=bpc*8
        else: raise ValueError('height unavailable')
    off=bpc*8-height; bits=set(); cols=0
    for i in range(count):
        rec=raw[header+i*slot:header+(i+1)*slot]; w=rec[0]
        for x in range((w+bpc-1)//bpc):
            d=rec[1+x*bpc:1+x*bpc+bpc]
            if len(d)==bpc:
                v=int.from_bytes(d,'big'); cols+=1
                bits.update(k for k in range(bpc*8) if v&(1<<k))
    sm=re.search(r'-(\d+)(?:\s|$)',p.stem); suffix=sm.group(1) if sm else 'none'
    print(f'{p.name} | suffix={suffix} | layout header={header} slot={slot} count={count} max_width={maxw} bytes_per_column={bpc} | height={height} bit_offset={off} | set_bits_0..{bpc*8-1}={sorted(bits)} | above: bit_offset-1={off-1 in bits}, bit_offset-2={off-2 in bits}')
for p in sorted(Path('Fonts/Axion').glob('OUTLINE*.BBM'))+[Path('Fonts/Axion/IMP-LUM-15.BBM')]: inspect(p)
