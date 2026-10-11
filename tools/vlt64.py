# VLT64 (VaultLib VLT64Hasher port): Attrib collection/class/attribute keys.
# Verified: h64("fe_locations") = 0xDDCD6BC23255E508, export ids = h64("<class>/<collection>").
M=(1<<64)-1
def h64(s, init=0xABCDEF0011223344):
  d=s.encode('ascii'); n=len(d); a=init; b=0x9e3779b97f4a7c13; c=n; pos=0; rem=n
  def rd(o,k): return d[o+k]
  while rem>0x17:
    o=pos
    l5=(a+sum(d[o+8+k]<<(8*k) for k in range(8)))&M
    b=(b+sum(d[o+16+k]<<(8*k) for k in range(8)))&M
    ai=(init+sum(d[o+k]<<(8*k) for k in range(8))-l5-b)&M ^ (b>>43)
    init=((l5-b-ai)&M) ^ ((ai<<9)&M)
    m2=((b-ai-init)&M) ^ (init>>8)
    ai=((ai-init-m2)&M) ^ (m2>>38)
    init=((init-m2-ai)&M) ^ ((ai<<23)&M)
    m2=((m2-ai-init)&M) ^ (init>>5)
    ai=((ai-init-m2)&M) ^ (m2>>35)
    b=((init-m2-ai)&M) ^ ((ai<<49)&M)
    m2=((m2-ai-b)&M) ^ (b>>11)
    init=((ai-b-m2)&M) ^ (m2>>12)
    ai=((b-m2-init)&M) ^ ((init<<18)&M)
    b=((m2-init-ai)&M) ^ (ai>>22)
    a=ai; pos+=24; rem-=24
  # the C# keeps 'init' (running) and 'manipulatedInit'(a) separately; after loop init var holds latest
  b=(b+(c&0xffffffff))&M
  o=pos
  for k in range(rem-1,-1,-1):
    v=d[o+k]
    if k>=16: b=(b+(v<<(8*(k-16)+8)))&M
    elif k>=8: a=(a+(v<<(8*(k-8))))&M
    else: init=(init+(v<<(8*k)))&M
  x1=((init-a-b)&M) ^ (b>>43)
  i2=((a-b-x1)&M) ^ ((x1<<9)&M)
  x2=((b-x1-i2)&M) ^ (i2>>8)
  a=((x1-i2-x2)&M) ^ (x2>>38)
  x1=((i2-x2-a)&M) ^ ((a<<23)&M)
  x2=((x2-a-x1)&M) ^ (x1>>5)
  a=((a-x1-x2)&M) ^ (x2>>35)
  x1=((x1-x2-a)&M) ^ ((a<<49)&M)
  x2=((x2-a-x1)&M) ^ (x1>>11)
  a=((a-x1-x2)&M) ^ (x2>>12)
  x1=((x1-x2-a)&M) ^ ((a<<18)&M)
  return ((x2-a-x1)&M) ^ (x1>>22)
