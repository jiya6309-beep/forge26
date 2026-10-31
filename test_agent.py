"""
Run the full agent once from the terminal, with a test flood zone near Nadiad/Anand.
Run:  python test_agent.py
"""
import time

from dotenv import load_dotenv

load_dotenv()

from agent.graph import run_agent  # noqa: E402

FLOOD_ZONE = {
    "type": "Polygon",
    "coordinates": [[
        [72.80, 22.60], [72.98, 22.60], [72.98, 22.76], [72.80, 22.76], [72.80, 22.60],
    ]],
}


def show(entries):
    for e in entries:
        print(f"[{e['step']:8}] {e['msg']}")


if __name__ == "__main__":
    start = time.time()
    final = run_agent("test", [FLOOD_ZONE], on_step=show)
    print(f"\nFinished in {time.time() - start:.0f}s | run id: {final.get('run_id')} "
          f"| purchase orders: {final.get('po_ids', [])}")
