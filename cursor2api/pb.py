"""Minimal protobuf + Connect-stream codec (reverse-engineered agent.v1)."""
import struct, gzip

def evar(v):
    o=bytearray()
    while True:
        x=v&0x7f; v>>=7
        o.append(x|0x80 if v else x)
        if not v: return bytes(o)
def dvar(b,i):
    v=s=0
    while True:
        x=b[i]; i+=1; v|=(x&0x7f)<<s; s+=7
        if not x&0x80: return v,i

def parse(buf):
    i=0; t=[]
    while i<len(buf):
        tag,i=dvar(buf,i); fn=tag>>3; wt=tag&7
        if wt==0: v,i=dvar(buf,i); t.append((fn,0,v))
        elif wt==2: l,i=dvar(buf,i); t.append((fn,2,buf[i:i+l])); i+=l
        elif wt==5: t.append((fn,5,buf[i:i+4])); i+=4
        elif wt==1: t.append((fn,1,buf[i:i+8])); i+=8
        else: raise ValueError("wiretype %d"%wt)
    return t

def emit(toks):
    o=bytearray()
    for fn,wt,val in toks:
        o+=evar((fn<<3)|wt)
        if wt==0: o+=evar(val)
        elif wt==2: o+=evar(len(val))+val
        else: o+=val
    return bytes(o)

def msg(**kw):
    """msg(f1=b'..', f2=1) -> encoded. bytes/str -> wt2, int -> varint, bool -> varint"""
    toks=[]
    for k,v in kw.items():
        fn=int(k[1:])
        if isinstance(v,bool): toks.append((fn,0,1 if v else 0))
        elif isinstance(v,int): toks.append((fn,0,v))
        elif isinstance(v,str): toks.append((fn,2,v.encode()))
        elif isinstance(v,bytes): toks.append((fn,2,v))
        elif isinstance(v,(list,tuple)):
            for e in v:
                toks.append((fn,2,e.encode() if isinstance(e,str) else e))
        else: raise TypeError(k)
    return emit(toks)

def get(buf,fn):
    for f,wt,v in parse(buf):
        if f==fn and wt==2: return v
    return None
def getall(buf,fn):
    return [v for f,wt,v in parse(buf) if f==fn and wt==2]
def getvar(buf,fn):
    for f,wt,v in parse(buf):
        if f==fn and wt==0: return v
    return None

def edit_path(buf,path,newval):
    out=[]; done=False
    for fn,wt,val in parse(buf):
        if not done and fn==path[0] and wt==2:
            out.append((fn,2,newval if len(path)==1 else edit_path(val,path[1:],newval)))
            done=True
        else: out.append((fn,wt,val))
    if not done: raise KeyError(path)
    return emit(out)

# ---- Connect streaming envelope ----
def frame(payload, flag=0):
    return bytes([flag])+struct.pack(">I",len(payload))+payload

def deframe(buf):
    """yields (flag, payload); returns leftover via generator .send? -> use helper class"""
    out=[]; i=0
    while len(buf)-i>=5:
        flag=buf[i]; ln=struct.unpack(">I",buf[i+1:i+5])[0]
        if len(buf)-i-5<ln: break
        p=buf[i+5:i+5+ln]; i+=5+ln
        if flag&0x01:
            try: p=gzip.decompress(p)
            except Exception: pass
        out.append((flag,p))
    return out, buf[i:]
