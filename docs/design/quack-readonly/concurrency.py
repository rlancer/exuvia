"""Readers and a writer at once under the policy. Usage: concurrency.py PORT DATA_PATH"""
import multiprocessing as mp
import os
import sys
import time

READERS, ROUNDS, APPENDS = 6, 25, 60


def writer(port, data, done):
    from common import WRITER_TOKEN, lake_client
    w = lake_client(port, WRITER_TOKEN, data)
    for i in range(APPENDS):
        w.execute(f"INSERT INTO lake.prices VALUES (DATE '2030-01-01' + {i}, 'W', {i})")
        time.sleep(0.05)
    done.set()


def reader(port, data, n, out):
    from common import READER_TOKEN, lake_client
    errors, last, seen = [], -1, 0
    try:
        r = lake_client(port, READER_TOKEN, data, read_only=True)
        for _ in range(ROUNDS):
            c = r.execute("SELECT count(*) FROM lake.prices").fetchone()[0]
            if c < last:
                errors.append(f"count went down {last} -> {c}")
            last = c
            r.execute("SELECT max(d) FROM lake.prices WHERE ticker = 'W'").fetchall()
            r.execute("SELECT count(*) FROM lake.snapshots()").fetchall()
            seen += 1
    except Exception as e:
        errors.append(str(e)[:400])
    out.put((n, seen, last, errors))


if __name__ == "__main__":
    port, data = int(sys.argv[1]), sys.argv[2]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import WRITER_TOKEN, lake_client
    setup = lake_client(port, WRITER_TOKEN, data)
    setup.execute("CREATE TABLE lake.prices (d DATE, ticker VARCHAR, px DOUBLE)")
    setup.execute("INSERT INTO lake.prices SELECT DATE '2020-01-01' + i::INT, 'A', i FROM range(1000) t(i)")
    done, out = mp.Event(), mp.Queue()
    t = time.time()
    procs = [mp.Process(target=writer, args=(port, data, done))]
    procs += [mp.Process(target=reader, args=(port, data, n, out)) for n in range(READERS)]
    for p in procs:
        p.start()
    results = [out.get(timeout=300) for _ in range(READERS)]
    for p in procs:
        p.join(timeout=120)
    final = setup.execute("SELECT count(*) FROM lake.prices").fetchone()[0]
    bad = 0
    for n, seen, last, errors in sorted(results):
        bad += len(errors)
        print(f"reader {n}: {seen} rounds, last count {last}, errors {errors[:2]}")
    print(f"final rows {final} (expected {1000 + APPENDS}); {READERS} readers x {ROUNDS} rounds x 3 queries "
          f"alongside {APPENDS} writer commits in {time.time() - t:.1f}s; reader errors: {bad}")
    os._exit(0)
