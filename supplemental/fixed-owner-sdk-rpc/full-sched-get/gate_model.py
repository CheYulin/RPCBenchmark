"""Offline linearization model only; this module never imports BCC."""
OPEN = 1 << 63
PHASE = 1 << 62
FAULT = 1 << 61
COUNT = FAULT - 1


class Gate:
    def __init__(self, cpus=4):
        self.words = [0] * cpus
        self.mode = 0
        self.sealed = set()

    def open(self, mode, fail_after=None):
        if mode not in (1, 2) or mode in self.sealed or self.mode:
            raise ValueError('invalid or repeated open')
        if any(word & (OPEN | FAULT) for word in self.words):
            raise ValueError('gate not closed')
        for cpu in range(len(self.words)):
            if fail_after == cpu:
                raise ValueError('partial open')
            self.words[cpu] |= OPEN | (PHASE if mode == 2 else 0)
        self.mode = mode

    def prepare(self, cpu):
        return self.words[cpu]

    @staticmethod
    def eligible(word, mode):
        return bool(word & OPEN) and not word & FAULT and bool(word & PHASE) == (mode == 2)

    def enter(self, cpu, mode, observed=None, fallback_pause=None, after_increment=None):
        observed = self.words[cpu] if observed is None else observed
        if not self.eligible(observed, mode):
            return None
        if observed & COUNT == COUNT:
            self.words[cpu] |= FAULT
            return None
        if fallback_pause:
            fallback_pause()
        self.words[cpu] += 1
        if after_increment:
            after_increment()
        after = self.words[cpu]
        if after & FAULT:
            self.words[cpu] -= 1
            return None
        if self.eligible(after, mode):
            return [cpu, mode, True]
        self.words[cpu] -= 1
        return None

    def leave(self, lease):
        if lease is None or not lease[2] or not self.words[lease[0]] & COUNT:
            raise ValueError('invalid/double release')
        self.words[lease[0]] -= 1
        lease[2] = False

    def seal(self, mode):
        self.mode = 0
        self.sealed.add(mode)
        for cpu in range(len(self.words)):
            self.words[cpu] &= ~OPEN
        return self.drained()

    def drained(self):
        return not any(word & (OPEN | FAULT | COUNT) for word in self.words)

    def mutate(self, lease, target, value):
        if lease is None or not lease[2]:
            raise ValueError('map write without a lease')
        target.append(value)


class UnsafeLegacyGate(Gate):
    """Behavioral RED: cached enable grants a late lease; close discards count."""
    def enter(self, cpu, mode, observed=None, fallback_pause=None, after_increment=None):
        observed = self.words[cpu] if observed is None else observed
        if not observed & OPEN:
            return None
        self.words[cpu] += 1
        if after_increment:
            after_increment()
        return [cpu, mode, True]

    def seal(self, mode):
        self.mode = 0
        self.sealed.add(mode)
        self.words = [word & PHASE for word in self.words]
        return True


class Reservation:
    """Bounded-CAS model; errors are writes under the caller's existing lease."""
    def __init__(self, value=0):
        self.value = value

    def reserve(self, gate, lease, bad, code, interleave=None, legacy_seventeenth_try=False):
        before = self.value
        for attempt in range(17 if legacy_seventeenth_try else 16):
            if before == (1 << 64) - 1:
                gate.mutate(lease, bad, code)
                return None
            if interleave:
                interleave(self, attempt)
            actual = self.value
            if actual == before:
                self.value = before + 1
                return before
            before = actual
        gate.mutate(lease, bad, code)
        return None
