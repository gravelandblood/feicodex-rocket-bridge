#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import queue
import time
from pathlib import Path

from appserver_client import AppServerError, CodexAppServerClient

HARD_MAX_OUTER_TIMEOUT_SEC = 300
HARD_MAX_ATTEMPTS = 3
APP_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smoke test for codex app-server bridge client.")
    p.add_argument("--cwd", default=str(APP_DIR), help="Thread cwd.")
    p.add_argument("--model", default="gpt-5.3-codex", help="Model override.")
    p.add_argument("--sandbox", default="danger-full-access", help="Sandbox mode for thread/start.")
    p.add_argument("--prompt", default="reply with one word only: pong", help="Prompt text.")
    p.add_argument("--timeout-sec", type=int, default=120, help="Turn completion timeout.")
    p.add_argument("--outer-timeout-sec", type=int, default=180, help="Hard timeout per attempt.")
    p.add_argument("--max-attempts", type=int, default=2, help="Retry limit for failed attempts.")
    p.add_argument("--retry-backoff-sec", type=int, default=2, help="Delay between retries.")
    return p.parse_args()


def _run_once(args: argparse.Namespace) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

    client = CodexAppServerClient()
    try:
        init = client.start()
        thread_start = client.thread_start(
            cwd=str(Path(args.cwd).resolve()),
            model=args.model,
            sandbox=args.sandbox,
            approval_policy="never",
            personality="pragmatic",
        )
        thread = thread_start.get("thread") if isinstance(thread_start.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise AppServerError(f"thread/start returned no thread id: {thread_start}")

        turn_start = client.turn_start(thread_id=thread_id, text=args.prompt)
        turn = turn_start.get("turn") if isinstance(turn_start.get("turn"), dict) else {}
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            raise AppServerError(f"turn/start returned no turn id: {turn_start}")

        done = client.wait_for_turn_completion(thread_id=thread_id, turn_id=turn_id, timeout_sec=args.timeout_sec)
        thread_after = client.thread_read(thread_id=thread_id, include_turns=False).get("thread", {})

        summary = {
            "ok": True,
            "initialize": init,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "turn_status": done.turn_status,
            "thread_status": thread_after.get("status"),
            "assistant_text": done.text,
            "turn_error": done.error,
        }
        return summary
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        client.stop()


def _worker(args_dict: dict, result_q: mp.Queue) -> None:
    args = argparse.Namespace(**args_dict)
    result_q.put(_run_once(args))


def _run_with_guards(args: argparse.Namespace) -> dict:
    max_attempts = min(max(1, int(args.max_attempts)), HARD_MAX_ATTEMPTS)
    outer_timeout_sec = min(max(10, int(args.outer_timeout_sec)), HARD_MAX_OUTER_TIMEOUT_SEC)
    retry_backoff_sec = max(0, int(args.retry_backoff_sec))
    args_dict = vars(args)

    last_result: dict = {"ok": False, "error": "unknown"}
    for attempt in range(1, max_attempts + 1):
        result_q: mp.Queue = mp.Queue(maxsize=1)
        proc = mp.Process(target=_worker, args=(args_dict, result_q), daemon=True)
        start_ts = time.time()
        proc.start()
        proc.join(timeout=outer_timeout_sec)
        elapsed = round(time.time() - start_ts, 3)

        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=3)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=1)
            last_result = {
                "ok": False,
                "error": f"outer timeout exceeded ({outer_timeout_sec}s)",
                "attempt": attempt,
                "attempt_elapsed_sec": elapsed,
            }
        else:
            try:
                result = result_q.get_nowait()
            except queue.Empty:
                result = {"ok": False, "error": f"worker exited without result (exit_code={proc.exitcode})"}
            if not isinstance(result, dict):
                result = {"ok": False, "error": "worker returned invalid result"}
            result["attempt"] = attempt
            result["attempt_elapsed_sec"] = elapsed
            last_result = result
            if bool(result.get("ok")):
                break

        if attempt < max_attempts and retry_backoff_sec > 0:
            time.sleep(retry_backoff_sec)

    last_result["attempts_used"] = int(last_result.get("attempt", max_attempts))
    last_result["max_attempts"] = max_attempts
    last_result["outer_timeout_sec"] = outer_timeout_sec
    return last_result


def main() -> int:
    args = parse_args()
    summary = _run_with_guards(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if bool(summary.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
