"""je 變體的 `BrowserPort`——共用層碰瀏覽器的唯一介面，在包裝層「失敗就丟例外」之下。

`webrunner_je_only._require_wrapper_contract` 開機時把 je_web_runner 的失敗政策釘成
丟例外（`architecture.md` §8.222 G8）。在那之前，這個 port 的好幾個方法是靠
包裝層「失敗回 None」才寫得對的；政策一換，「沒找到」「讀不到」就會變成例外。這一檔
逐一釘住在新語意下每個方法回什麼——替身 `wr` 就照新語意行為：真的失敗才丟、
`find_element` 找不到時丟 `NoSuchElementException`、`find_elements` 找不到時回空清單。

不開瀏覽器、不 import selenium 的驅動 API，只用替身記錄 port 對 `wr` 說了什麼。
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import webrunner_je_only as je  # noqa: E402
import _webrunner_shared as ws  # noqa: E402


class _NoSuchElement(Exception):
    """`find_element` 在新語意下找不到時丟的例外（替身，與 selenium 同名同義）。"""


class _FakeWrapper:
    """照「失敗就丟例外」行為的 `wr` 替身，記錄每一次呼叫。"""

    def __init__(self):
        self.calls: list = []
        self.hits: dict = {}
        self.url: str | None = "https://example.invalid/image"
        self.title: str | None = "Image"

    def find_elements(self, test_object):
        self.calls.append(("find_elements", test_object.test_object_name))
        return list(self.hits.get(test_object.test_object_name, []))

    def find_element(self, test_object):
        self.calls.append(("find_element", test_object.test_object_name))
        hits = self.hits.get(test_object.test_object_name)
        if not hits:
            raise _NoSuchElement(test_object.test_object_name)
        return hits[0]

    def get_current_url(self):
        return self.url

    def get_title(self):
        return self.title


@pytest.fixture
def fake_wr(monkeypatch):
    wrapper = _FakeWrapper()
    monkeypatch.setattr(je, "wr", wrapper)
    return wrapper


# ---------------------------------------------------------------------------
# 一、查元素：「沒找到」不是錯誤
# ---------------------------------------------------------------------------

def test_find_element_xpath_returns_the_first_hit(fake_wr):
    fake_wr.hits["//x"] = ["第一個", "第二個"]
    assert je.port.find_element_xpath("//x") == "第一個"


def test_find_element_xpath_returns_none_when_nothing_matches(fake_wr):
    """**新語意下最容易壞的一格。** 走 `wr.find_element` 的話，找不到會丟例外——
    `_do_login` 一秒三次輪詢 email 欄位，每一次都變成一個例外；政策釘下去之前則是每一次
    都往 `WEBRunner.log` 寫一筆 ERROR（§8.222 G9 那 146 筆的來源之一）。"""
    assert je.port.find_element_xpath("//沒有") is None


def test_find_element_xpath_never_asks_the_raising_lookup(fake_wr):
    """反方向：確認它問的是 `find_elements`，不是靠 `find_element` 的例外判斷「沒有」。

    只有上面兩支的話，把實作改回 `find_element` ＋ `except: return None` 也會全綠——
    行為一樣，但每一次沒找到又回到「丟一次例外、包裝層記一次失敗」。
    """
    je.port.find_element_xpath("//x")
    assert [name for name, _ in fake_wr.calls] == ["find_elements"], fake_wr.calls


def test_find_elements_xpath_turns_a_none_into_an_empty_list(fake_wr, monkeypatch):
    """包裝層在舊的失敗政策下回 None；呼叫端拿它跑 `for`，所以一律給清單。"""
    monkeypatch.setattr(fake_wr, "find_elements", lambda _t: None)
    assert je.port.find_elements_xpath("//x") == []


# ---------------------------------------------------------------------------
# 二、讀頁面：與 selenium 變體同一個回傳形狀
# ---------------------------------------------------------------------------

def test_the_title_is_never_none(fake_wr):
    """selenium 變體回 `driver.title or ""`；這一邊原本原樣回包裝層的值（可能是
    None）。今天唯一的呼叫端 `_is_chrome_crash_page` 自己寫了 `or ""` 所以沒出事，但那是
    呼叫端的性質：下一個拿它直接 `.lower()` 的共用函式會丟 `AttributeError`，而那不在
    `TRANSPORT_ERRORS` 裡。port 的回傳形狀應該兩邊一樣，不該靠呼叫端記得。"""
    fake_wr.title = None
    assert je.port.get_title() == ""
    fake_wr.title = "Aw, Snap!"
    assert je.port.get_title() == "Aw, Snap!"


def test_the_current_url_is_never_none(fake_wr):
    fake_wr.url = None
    assert je.port.current_url() == ""


# ---------------------------------------------------------------------------
# 三、`wait_until`：瀏覽器不見了就不要再等
# ---------------------------------------------------------------------------

def test_wait_until_reraises_a_gone_browser_instead_of_waiting_it_out(monkeypatch):
    """包裝層改成丟例外之後，瀏覽器整個不見了也會丟進 `wait_until` 的述詞裡。

    吞下來的話，`_do_login` 會對著一個死掉的 session 等滿 30 秒、snap、回 False，
    `with_retry("login")` 再重來一次——三次一共一分半，原因卻記成「登入失敗」。
    selenium 變體的 `WebDriverWait.until` 也只吞「找不到元素」。
    """
    slept: list = []
    monkeypatch.setattr(je.time, "sleep", slept.append)

    class InvalidSessionIdException(Exception):
        pass

    def gone():
        raise InvalidSessionIdException("invalid session id")

    assert ws.is_browser_gone_error(InvalidSessionIdException("x")), (
        "前提：這個例外名字在 gone 名單裡")
    with pytest.raises(InvalidSessionIdException):
        je.wait_until(gone, timeout=30.0)
    assert slept == [], "瀏覽器都不見了還在等"


def test_wait_until_still_treats_an_ordinary_failure_as_not_yet(monkeypatch):
    """必須放行的那一面：一般的失敗（元素還沒出現、頁面還在切換）照舊當成「還沒好」。

    少了這一支，把 `wait_until` 改成「任何例外都往上丟」也會讓上面那支通過，而那會讓
    每一個還在載入的頁面都變成一次登入失敗。
    """
    clock = {"now": 0.0}
    monkeypatch.setattr(je.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(je.time, "sleep",
                        lambda secs: clock.__setitem__("now", clock["now"] + secs))
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ValueError("還在載入")
        return True

    assert je.wait_until(flaky, timeout=30.0, poll=1.0) is True
    assert attempts["n"] == 3


# ---------------------------------------------------------------------------
# 四、像人的輸入：hover → 停頓 → 點、登入時擺動滑鼠、ActionChains 的 Escape
# （擁有者 2026-10-01 裁定移植，§8.222 G3／G4／G5）
# ---------------------------------------------------------------------------

class _ActionWrapper(_FakeWrapper):
    """加上包裝層 ActionChains 佇列的替身。`fail_on` 指定的那一步丟例外。"""

    def __init__(self, fail_on: str | None = None):
        super().__init__()
        self.fail_on = fail_on
        self.active = _FakeElement("active")

    def _record(self, step, *args):
        self.calls.append((step, *args))
        if step == self.fail_on:
            raise RuntimeError(f"{step} 失敗")

    def move_to_element(self, element):
        self._record("move_to_element", element.name)

    def pause(self, seconds):
        self._record("pause", seconds)

    def left_click(self, on_element=None):
        self._record("left_click", on_element)

    def move_by_offset(self, x, y):
        self._record("move_by_offset", x, y)

    def send_keys(self, keys):
        self._record("send_keys", tuple(keys))

    def perform(self):
        self._record("perform")

    def reset_actions(self):
        self._record("reset_actions")

    def execute_script(self, script, *args):
        names = tuple(getattr(a, "name", a) for a in args)
        self._record("execute_script", script, names)
        if "activeElement" in script:
            return self.active
        return None


class _FakeElement:
    def __init__(self, name):
        self.name = name
        self.keys: list = []

    def send_keys(self, *keys):
        self.keys.extend(keys)

    def click(self):
        raise AssertionError("`port.click` 不該走純 element.click()")


def _steps(wrapper) -> list[str]:
    return [call[0] for call in wrapper.calls]


def test_click_hovers_pauses_then_clicks(monkeypatch):
    """跟 selenium 變體同一個序列：滑過去、停、按、一次送出。不走 JS、不走純 click。"""
    wrapper = _ActionWrapper()
    monkeypatch.setattr(je, "wr", wrapper)
    je.port.click(_FakeElement("鈕"), pause=0.12)
    assert wrapper.calls == [("move_to_element", "鈕"), ("pause", 0.12),
                             ("left_click", None), ("perform",)], wrapper.calls


def test_click_uses_the_default_pause_like_the_selenium_variant(monkeypatch):
    wrapper = _ActionWrapper()
    monkeypatch.setattr(je, "wr", wrapper)
    je.port.click(_FakeElement("鈕"))
    assert ("pause", 0.15) in wrapper.calls


@pytest.mark.parametrize("fail_on", ["move_to_element", "perform"])
def test_a_failed_action_chain_falls_back_to_a_js_click(monkeypatch, fail_on):
    """ActionChains 失敗 → 清掉佇列 → JS 點選同一個元素。

    `move_to_element` 那一格是「排隊排到一半就失敗」：不清佇列的話，殘留的動作會跟著
    下一次不相干的 `perform()` 一起送出。`perform` 那一格是最常見的「送出去被拒」。
    """
    wrapper = _ActionWrapper(fail_on=fail_on)
    monkeypatch.setattr(je, "wr", wrapper)
    je.port.click(_FakeElement("鈕"))
    steps = _steps(wrapper)
    assert "reset_actions" in steps, f"失敗之後沒有清佇列：{steps}"
    assert steps[-1] == "execute_script", steps
    assert wrapper.calls[-1][1] == "arguments[0].click();"
    assert wrapper.calls[-1][2] == ("鈕",)
    assert steps.index("reset_actions") < steps.index("execute_script")


def test_a_failing_queue_reset_does_not_stop_the_js_fallback(monkeypatch):
    """清佇列本身失敗（瀏覽器多半已經不在）也要繼續走 JS 退路——真正的原因由那一步
    往上丟，不是由清理那一步。"""
    wrapper = _ActionWrapper(fail_on="perform")
    original = wrapper._record

    def record(step, *args):
        if step == "reset_actions":
            wrapper.calls.append((step,))
            raise RuntimeError("reset 也失敗")
        return original(step, *args)

    monkeypatch.setattr(wrapper, "_record", record)
    monkeypatch.setattr(je, "wr", wrapper)
    je.port.click(_FakeElement("鈕"))
    assert _steps(wrapper)[-1] == "execute_script"


def test_the_login_wiggle_really_moves_the_mouse(monkeypatch):
    """兩三段隨機位移、每段之後停一下、最後一次送出（數字與 selenium 變體相同）。"""
    wrapper = _ActionWrapper()
    monkeypatch.setattr(je, "wr", wrapper)
    je.port.mouse_wiggle()
    moves = [c for c in wrapper.calls if c[0] == "move_by_offset"]
    pauses = [c for c in wrapper.calls if c[0] == "pause"]
    assert 2 <= len(moves) <= 3 and len(pauses) == len(moves), wrapper.calls
    assert all(-60 <= c[1] <= 60 and -40 <= c[2] <= 40 for c in moves), moves
    assert all(0.08 <= c[1] <= 0.2 for c in pauses), pauses
    assert _steps(wrapper)[-1] == "perform"


def test_a_failed_wiggle_is_swallowed_and_cleans_up(monkeypatch):
    """擺動失敗不能讓登入失敗（selenium 變體也是吞掉），但要清掉排到一半的佇列。"""
    wrapper = _ActionWrapper(fail_on="perform")
    monkeypatch.setattr(je, "wr", wrapper)
    je.port.mouse_wiggle()
    assert _steps(wrapper)[-1] == "reset_actions", wrapper.calls


def test_the_login_flow_wiggles_before_touching_the_form(monkeypatch):
    """`_do_login` 在關掉同意橫幅之後、碰 email 欄位之前擺動滑鼠（與 selenium 變體同位置）。"""
    order: list = []
    monkeypatch.setattr(je.port, "current_url", lambda: "https://example.invalid/login")
    monkeypatch.setattr(ws, "reject_cookies",
                        lambda _port, timeout=0: order.append("cookies"))
    monkeypatch.setattr(je.port, "mouse_wiggle", lambda: order.append("wiggle"))

    def no_email(*_a, **_k):
        order.append("email")
        return False

    monkeypatch.setattr(je, "wait_until", no_email)
    monkeypatch.setattr(ws, "snap", lambda *_a, **_k: None)
    assert je._do_login("someone@example.invalid", "pw") is False
    assert order == ["cookies", "wiggle", "email"], order


def test_escape_goes_through_action_chains_first(monkeypatch):
    """先送 ActionChains 的 Escape（像人按鍵盤一樣打給有焦點的東西），成功就不碰退路。"""
    wrapper = _ActionWrapper()
    monkeypatch.setattr(je, "wr", wrapper)
    assert je.port.press_escape() is True
    assert wrapper.calls == [("send_keys", (je.Keys.ESCAPE,)), ("perform",)], wrapper.calls
    assert wrapper.active.keys == []


def test_escape_falls_back_to_the_focused_element(monkeypatch):
    """ActionChains 送不出去 → 清佇列 → 對焦點元素 `send_keys`，仍回 True。"""
    wrapper = _ActionWrapper(fail_on="perform")
    monkeypatch.setattr(je, "wr", wrapper)
    assert je.port.press_escape() is True
    assert "reset_actions" in _steps(wrapper)
    assert wrapper.active.keys == [je.Keys.ESCAPE]


def test_escape_reports_failure_when_both_paths_fail(monkeypatch):
    """兩條路都送不出去 → 回 False（呼叫端靠這個決定要不要改走合成事件）。"""
    wrapper = _ActionWrapper(fail_on="perform")
    monkeypatch.setattr(je, "wr", wrapper)
    monkeypatch.setattr(wrapper, "execute_script",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("讀不到焦點")))
    assert je.port.press_escape() is False
