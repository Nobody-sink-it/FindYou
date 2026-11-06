"""Explicit bounded live check; never publishes and never prints keys or raw user comments."""

import argparse
import json
from pathlib import Path

from findyou.config import Settings
from findyou.errors import AppError
from findyou.runtime import Runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Allow one real platform discovery and model decision")
    parser.add_argument("--keyword", default="亲密关系 倾听")
    parser.add_argument("--output", default="agent-live-20261104.json")
    parser.add_argument("--video", default="")
    args = parser.parse_args()
    if not args.live:
        parser.error("Use --live explicitly; offline end-to-end checks are in tests/test_agent.py")
    rt = Runtime(Settings())
    if not rt.db.readiness()["ready"]:
        raise SystemExit("Initialize the current schema first")
    if rt.services.paused():
        raise SystemExit("Agent is paused; inspect its status before resuming")
    role = rt.identity.select_role()
    report = {"real_platform_requested": True, "real_model_configured": rt.settings.llm_provider != "fake",
              "platform_write": False, "role_version_id": role["id"]}
    try:
        cycle = rt.agent.cycle({"keyword": args.keyword, "video_ids": [args.video] if args.video else [],
            "max_videos": 1, "max_targets": 1, "comment_sort": "time",
            "include_subtitle": True, "role_version_id": role["id"],
            "objective": "寻找可以自然交流的倾听话题；只生成草稿，旧评论或语境不足时可跳过。"})
        report.update(status="completed", cycle=cycle)
        report["child_runs"] = [rt.agent.get_run(d["run_id"]) for d in cycle["decisions"]]
        if cycle["errors"]:
            report["status"] = "partial"
    except AppError as exc:
        report.update(status="failed", error=exc.as_dict())
    finally:
        report["real_model_succeeded"] = any(r["usage"].get("usage_known") and
            not r["usage"].get("simulated") for r in report.get("child_runs", []))
        folder = rt.settings.data_dir / "validation"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / Path(args.output).name
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        public_summary = {k: report.get(k) for k in ("status", "real_platform_requested", "real_model_configured",
                                                   "real_model_succeeded", "platform_write", "error")}
        public_summary["report"] = str(path.resolve())
        public_summary["child_run_count"] = len(report.get("child_runs", []))
        print(json.dumps(public_summary, ensure_ascii=False))
        rt.db.engine.dispose()


if __name__ == "__main__":
    main()
