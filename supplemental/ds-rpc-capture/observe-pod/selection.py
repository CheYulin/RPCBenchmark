"""Byte-identical frozen seed17/mod64 selection function; no BPF sampling flag."""
def selected(phase, arrival):
    mask = (1 << 64) - 1
    def mix(value):
        value = ((value ^ (value >> 30)) * 0xbf58476d1ce4e5b9) & mask
        value = ((value ^ (value >> 27)) * 0x94d049bb133111eb) & mask
        return value ^ (value >> 31)
    return mix(17 ^ mix(phase) ^ mix((arrival + 0x9e3779b97f4a7c15) & mask)) % 64 == 0
