VALID = 'FIXED_OWNER8_QUEUED_ARRIVAL_VALID'


class WindowDataError(RuntimeError):
    pass


def planned_rows():
    order = [(True, op, arm) for arm in ('brpc', 'coro') for op in ('get', 'set')]
    order += [(False, op, arm) for op in ('get', 'set') for arm in ('brpc', 'coro', 'coro', 'brpc')]
    return [dict(index=i, smoke=smoke, operation=op, arm=arm, backend=arm,
                 mode='enabled', status='NOT_RUN') for i, (smoke, op, arm) in enumerate(order, 1)]


def execute(rows, run_one, persist, verify_bindings):
    persist()
    for row in rows:
        row['status'] = 'STARTED'
        persist()
        try:
            try:
                run_one(row)
            finally:
                verify_bindings(row)
            assert row['status'] == VALID, 'Window returned without complete validation'
        except WindowDataError as error:
            row.update(status='FAILED_DATA', reason=str(error))
            persist()
            if row['smoke']:
                return
        except BaseException as error:
            row.update(status='FAILED_SAFETY', reason=repr(error))
            persist()
            raise
        persist()


def summarize(rows, load, decide):
    groups = []
    for operation in ('get', 'set'):
        cohort = [r for r in rows if not r['smoke'] and r['operation'] == operation]
        group = dict(operation=operation, case_indices=[r['index'] for r in cohort])
        if len(cohort) == 4 and all(r['status'] == VALID for r in cohort):
            group['decision'] = decide([load(row) for row in cohort])
        else:
            group.update(decision=None, status='INCOMPLETE_COHORT_NO_NUMERIC_COMPARISON')
        groups.append(group)
    return groups


def decode_terminal(lines):
    import json
    try:
        records = [json.loads(line[len('RESULT '):]) for line in lines if line.startswith('RESULT ')]
    except json.JSONDecodeError as error:
        raise WindowDataError('SDK terminal RESULT is malformed JSON') from error
    if len(records) != 1 or type(records[0]) is not dict:
        raise WindowDataError('SDK workflow did not emit one object terminal RESULT')
    return records[0]


def validate_data(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ValueError, FileNotFoundError) as error:
        raise WindowDataError(f'{type(error).__name__}: {error}') from error
