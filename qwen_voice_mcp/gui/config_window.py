"""Tkinter 配置中心（spec §13）。

布局对齐说明书 §3.1：API 配置 / 服务配置 / 操作按钮。

设计要点：

* ``tkinter`` 在 :func:`launch` 内部才导入，因此无显示环境下 **导入本模块** 不会失败
  （CLI 也会 import 它），只有真正启动窗口才会。
* 所有耗时操作（验证密钥、查询余额、刷新模型）放到工作线程，结果经 ``queue``
  回到主线程更新界面，避免 Tk 界面卡死。
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable

from qwen_voice_mcp.core.config_store import Options, load_options, save_options
from qwen_voice_mcp.core.model_registry import ModelRegistry
from qwen_voice_mcp.core.secret_manager import SecretManager, VaultError

__all__ = ["launch", "ConfigWindow"]


class ConfigWindow:
    """配置中心窗口。

    ``launch()`` 负责导入 tkinter 并驱动主循环；本类只关心界面与状态，
    因此可以在有显示环境时被直接实例化与测试。
    """

    def __init__(self, tk: Any, ttk: Any, messagebox: Any) -> None:
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox

        self.options: Options = load_options()
        self.secret = SecretManager()
        self.results: queue.Queue[tuple[str, Any]] = queue.Queue()

        self.root = tk.Tk()
        self.root.title("Qwen Voice MCP — 配置中心")
        self.root.minsize(560, 380)

        self._key_var = tk.StringVar(value=self._existing_key())
        self._status_var = tk.StringVar(value="● 未验证")
        self._show_key = tk.BooleanVar(value=False)
        self._transport_var = tk.StringVar(value=self.options.transport)
        self._port_var = tk.StringVar(value=str(self.options.port))
        self._voice_var = tk.StringVar(value=self.options.default_voice)
        self._threshold_var = tk.StringVar(value=f"{self.options.balance_threshold:.2f}")
        self._strict_var = tk.BooleanVar(value=self.options.strict_balance)

        self._build()
        self.root.after(100, self._drain_results)

    # ------------------------------------------------------------------ helpers

    def _existing_key(self) -> str:
        try:
            return self.secret.retrieve("DASHSCOPE_API_KEY") or ""
        except VaultError:
            return ""

    # ---------------------------------------------------------------------- UI

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        api = ttk.LabelFrame(outer, text="API 配置", padding=10)
        api.pack(fill="x", pady=(0, 10))

        ttk.Label(api, text="API Key:").grid(row=0, column=0, sticky="w")
        self._key_entry = ttk.Entry(api, textvariable=self._key_var, width=42, show="•")
        self._key_entry.grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(api, text="验证", command=self._on_verify).grid(row=0, column=2)
        ttk.Checkbutton(api, text="显示", variable=self._show_key,
                        command=self._toggle_key_visibility).grid(row=0, column=3, padx=(6, 0))
        ttk.Label(api, textvariable=self._status_var).grid(row=1, column=1, sticky="w", pady=(6, 0))
        api.columnconfigure(1, weight=1)

        svc = ttk.LabelFrame(outer, text="服务配置", padding=10)
        svc.pack(fill="x", pady=(0, 10))

        ttk.Label(svc, text="传输方式:").grid(row=0, column=0, sticky="w")
        transport = ttk.Frame(svc)
        transport.grid(row=0, column=1, sticky="w", padx=6)
        for value in ("stdio", "streamable-http"):
            ttk.Radiobutton(transport, text=value, value=value,
                            variable=self._transport_var).pack(side="left", padx=(0, 10))

        ttk.Label(svc, text="监听端口:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(svc, textvariable=self._port_var, width=12).grid(row=1, column=1, sticky="w",
                                                                   padx=6, pady=(6, 0))

        ttk.Label(svc, text="默认音色:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._voice_box = ttk.Combobox(svc, textvariable=self._voice_var, width=24)
        self._voice_box["values"] = self._voice_choices()
        self._voice_box.grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(svc, text="余额阈值:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(svc, textvariable=self._threshold_var, width=12).grid(row=3, column=1, sticky="w",
                                                                        padx=6, pady=(6, 0))
        ttk.Checkbutton(svc, text="严格模式（余额查询失败也拒绝执行）",
                        variable=self._strict_var).grid(row=4, column=1, sticky="w", padx=6, pady=(6, 0))
        svc.columnconfigure(1, weight=1)

        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        ttk.Button(actions, text="保存配置", command=self._on_save).pack(side="left")
        ttk.Button(actions, text="测试连接", command=self._on_verify).pack(side="left", padx=6)
        ttk.Button(actions, text="刷新模型", command=self._on_refresh_models).pack(side="left")
        ttk.Button(actions, text="清除密钥", command=self._on_clear).pack(side="left", padx=6)

    def _voice_choices(self) -> list[str]:
        voices = [self.options.default_voice, "longanyang", "longxiaochun", "longwan", "longhua"]
        seen: list[str] = []
        for voice in voices:
            if voice and voice not in seen:
                seen.append(voice)
        return seen

    def _toggle_key_visibility(self) -> None:
        self._key_entry.configure(show="" if self._show_key.get() else "•")

    # ------------------------------------------------------------------ actions

    def _on_save(self) -> None:
        try:
            options = self._collect_options()
        except ValueError as exc:
            self.messagebox.showerror("配置无效", str(exc))
            return

        key = self._key_var.get().strip()
        if key:
            # Spec §13: validate before storing, so a typo is never persisted.
            # A probe that could not reach the server is not proof of a bad key,
            # so the operator may choose to keep it.
            check = self._validate(key)
            if not check.ok and not self.messagebox.askyesno(
                "密钥未通过验证",
                f"{check.detail}\n\n仍要保存吗？\n"
                "（若只是网络不通，可以保存；若密钥填错，请先修正。）",
            ):
                self._status_var.set("● 未保存：密钥未通过验证")
                return

            try:
                self.secret.store("DASHSCOPE_API_KEY", key)
            except VaultError as exc:
                self.messagebox.showerror("密钥保存失败", str(exc))
                return

        try:
            path = save_options(options)
        except Exception as exc:  # noqa: BLE001
            self.messagebox.showerror("保存失败", str(exc))
            return

        self.options = options
        self._status_var.set(f"● 已保存 — {path}")

    @staticmethod
    def _validate(key: str) -> Any:
        """Probe the credential with the shared validator (never billable)."""
        from qwen_voice_mcp.core.balance_checker import validate_credential

        return validate_credential(key, timeout=load_options().balance_timeout_seconds)

    def _collect_options(self) -> Options:
        try:
            port = int(self._port_var.get())
            threshold = float(self._threshold_var.get())
        except ValueError as exc:
            raise ValueError("端口必须是整数，余额阈值必须是数字。") from exc

        merged = self.options.model_copy(update={
            "transport": self._transport_var.get(),
            "port": port,
            "default_voice": self._voice_var.get().strip(),
            "balance_threshold": threshold,
            "strict_balance": bool(self._strict_var.get()),
        })
        return Options(**merged.model_dump())

    def _on_verify(self) -> None:
        key = self._key_var.get().strip() or self._existing_key()
        if not key:
            self.messagebox.showwarning("缺少密钥", "请先填写 API Key。")
            return
        self._status_var.set("● 验证中…")
        self._run_in_thread("verify", lambda: self._probe(key))

    def _on_refresh_models(self) -> None:
        self._run_in_thread("models", self._probe_models)

    def _on_clear(self) -> None:
        try:
            removed = self.secret.delete("DASHSCOPE_API_KEY")
        except VaultError as exc:
            self.messagebox.showerror("删除失败", str(exc))
            return
        self._key_var.set("")
        self._status_var.set("● 已清除密钥" if removed else "● 加密库中本来就没有密钥")

    # ------------------------------------------------------------- worker thread

    @staticmethod
    def _probe(key: str) -> dict[str, Any]:
        from qwen_voice_mcp.core.balance_checker import BalanceChecker

        options = load_options()
        state = BalanceChecker(key, timeout=options.balance_timeout_seconds).query()
        if state.balance is None:
            detail = "无法获取余额（接口不可用或密钥无权限）"
            if state.last_error:
                detail = f"{detail}: {state.last_error}"
            return {"ok": False, "text": detail}
        return {"ok": True, "text": f"已验证 — 账户余额 ¥{state.balance:.2f}"}

    @staticmethod
    def _probe_models() -> dict[str, Any]:
        registry = ModelRegistry(default_model=load_options().default_model)
        names = registry.names()
        return {"ok": True, "text": f"已加载 {len(names)} 个模型", "models": names}

    def _run_in_thread(self, tag: str, work: Callable[[], Any]) -> None:
        def runner() -> None:
            try:
                self.results.put((tag, work()))
            except Exception as exc:  # noqa: BLE001
                self.results.put((tag, {"ok": False, "text": f"{type(exc).__name__}: {exc}"}))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_results(self) -> None:
        try:
            while True:
                tag, payload = self.results.get_nowait()
                self._apply_result(tag, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_results)

    def _apply_result(self, tag: str, payload: Any) -> None:
        if tag == "verify":
            prefix = "● " if payload.get("ok") else "● "
            self._status_var.set(prefix + str(payload.get("text", "")))
        elif tag == "models":
            models = payload.get("models") or []
            if models:
                self._voice_box["values"] = self._voice_choices()
            self._status_var.set(f"● {payload.get('text', '')}")

    # --------------------------------------------------------------------- run

    def run(self) -> int:
        self.root.mainloop()
        return 0


def launch() -> int:
    """Open the configuration window. Returns a process exit code."""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print(
            "无法导入 tkinter：当前 Python 缺少 Tk 支持。\n"
            "可改用命令行配置：qwen-voice-mcp config set-key sk-xxxx",
            file=__import__("sys").stderr,
        )
        return 1

    try:
        window = ConfigWindow(tk, ttk, messagebox)
    except tk.TclError as exc:
        print(
            f"无法创建窗口（可能没有可用显示环境）: {exc}\n"
            "可改用命令行配置：qwen-voice-mcp config set-key sk-xxxx",
            file=__import__("sys").stderr,
        )
        return 1
    return window.run()
