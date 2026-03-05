from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from typing import Optional

import requests

from src.auth.cas_client import ZJUAuthClient
from src.common.account import resolve_credentials
from src.common.course_meta import (
    course_teachers,
    query_course_detail,
)
from src.common.http import get_thread_session
from src.scan.live_check import check_course_live_status

def query_course_worker(
    token: str,
    timeout: int,
    retries: int,
    course_id: int,
) -> tuple[int, Optional[dict]]:
    session = get_thread_session()
    data = query_course_detail(session, token, timeout, course_id, retries)
    return course_id, data


def run_scan(args: argparse.Namespace) -> int:
    username, password, cred_error = resolve_credentials(args.username, args.password)
    if cred_error:
        print(f"Credential error: {cred_error}", file=sys.stderr)
        return 1

    auth = ZJUAuthClient(timeout=args.timeout, tenant_code=args.tenant_code)
    login_session = requests.Session()

    try:
        token = auth.login_and_get_token(
            session=login_session,
            username=username,
            password=password,
            center_course_id=args.center,
            authcode=args.authcode,
        )
    except Exception as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    if not token:
        print("Login succeeded but token is empty; cannot continue scan.", file=sys.stderr)
        return 1

    start_id = args.center - args.radius
    end_id = args.center + args.radius
    course_ids = list(range(start_id, end_id + 1))
    require_live = bool(getattr(args, "require_live", False))
    live_check_timeout = max(0.0, float(getattr(args, "live_check_timeout", 30.0)))
    live_check_interval = max(0.0, float(getattr(args, "live_check_interval", 2.0)))

    found: list[dict] = []
    matched_candidates: list[dict] = []
    live_check_failures: list[dict] = []
    live_checked_candidates = 0
    scanned = 0

    def handle_result(cid: int, data: Optional[dict]) -> None:
        nonlocal scanned
        scanned += 1
        if not data:
            if args.verbose:
                print(f"[{cid}] no data")
            return

        title = data.get("title", "")
        teachers = course_teachers(data)

        if title == args.title and args.teacher in teachers:
            candidate = {
                "course_id": cid,
                "title": title,
                "teachers": teachers,
            }
            if require_live:
                matched_candidates.append(candidate)
                if args.verbose:
                    print(
                        f"[CANDIDATE] course_id={cid} title={title} "
                        f"teachers={','.join(teachers)} (pending live check)"
                    )
            else:
                print(f"[MATCH] course_id={cid} title={title} teachers={','.join(teachers)}")
                found.append(candidate)
        elif args.verbose:
            print(f"[{cid}] title={title} teachers={','.join(teachers)}")

    if args.workers <= 1:
        single_session = requests.Session()
        for cid in course_ids:
            data = query_course_detail(single_session, token, args.timeout, cid, args.retries)
            handle_result(cid, data)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            future_map = {
                pool.submit(query_course_worker, token, args.timeout, args.retries, cid): cid
                for cid in course_ids
            }
            for fut in concurrent.futures.as_completed(future_map):
                cid = future_map[fut]
                try:
                    result_cid, data = fut.result()
                except Exception:
                    result_cid, data = cid, None
                handle_result(result_cid, data)

    if require_live:
        matched_candidates.sort(key=lambda x: x["course_id"])
        live_session = requests.Session()
        for candidate in matched_candidates:
            live_checked_candidates += 1
            live_result = check_course_live_status(
                session=live_session,
                token=token,
                timeout=args.timeout,
                tenant_code=args.tenant_code,
                course_id=int(candidate["course_id"]),
                max_wait_sec=live_check_timeout,
                interval_sec=live_check_interval,
            )
            if live_result.checked and live_result.is_live:
                print(
                    f"[MATCH] course_id={candidate['course_id']} title={candidate['title']} "
                    f"teachers={','.join(candidate['teachers'])} live=直播中"
                )
                found.append(candidate)
                continue

            if live_result.checked:
                if args.verbose:
                    print(
                        f"[FILTERED-NOT-LIVE] course_id={candidate['course_id']} "
                        f"title={candidate['title']} teachers={','.join(candidate['teachers'])}"
                    )
                continue

            failure = {
                "course_id": candidate["course_id"],
                "title": candidate["title"],
                "teacher": args.teacher,
                "attempts": live_result.attempts,
                "elapsed_sec": live_result.elapsed_sec,
                "last_error": live_result.last_error,
                "hint": live_result.hint,
            }
            live_check_failures.append(failure)
            print(
                f"[LIVE-CHECK-FAIL] course_id={candidate['course_id']} title={candidate['title']} "
                f"teacher={args.teacher} attempts={live_result.attempts} "
                f"elapsed_sec={live_result.elapsed_sec} last_error={live_result.last_error} "
                f"hint={live_result.hint}",
                file=sys.stderr,
            )

    found.sort(key=lambda x: x["course_id"])

    print(
        json.dumps(
            {
                "mode": "scan",
                "center": args.center,
                "radius": args.radius,
                "scanned": scanned,
                "teacher": args.teacher,
                "title": args.title,
                "require_live": require_live,
                "live_check_timeout_sec": live_check_timeout,
                "live_check_interval_sec": live_check_interval,
                "live_checked_candidates": live_checked_candidates,
                "live_check_failures": live_check_failures,
                "matches": found,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
