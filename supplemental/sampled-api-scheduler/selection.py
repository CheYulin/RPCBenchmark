MASK=(1<<64)-1
def mix(value):
    value&=MASK
    value=((value^(value>>30))*0xbf58476d1ce4e5b9)&MASK
    value=((value^(value>>27))*0x94d049bb133111eb)&MASK
    return value^(value>>31)
def selected(phase,arrival):
    if phase not in (1,2):raise ValueError('only warm1 and measure2 phases')
    return mix(17^mix(phase)^mix((arrival+0x9e3779b97f4a7c15)&MASK))%64==0
