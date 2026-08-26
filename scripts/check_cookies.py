#!/usr/bin/env python3
"""Soi file cookies.txt trước khi đẩy lên Modal.

Nạp nhầm một file hỏng thì backend vẫn chạy, chỉ là yt-dlp lặng lẽ bị YouTube
chặn y như cũ — không có gì để lần ra nguyên nhân. Thà chặn ngay ở đây.

Dùng:  python3 scripts/check_cookies.py cookies.txt
"""
import sys
import time

# Cookie đăng nhập của Google. Thiếu sạch nhóm này thì file chỉ là cookie khách
# vãng lai, nạp lên cũng vô ích.
AUTH_COOKIES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
}


def check(path):
    problems, notes = [], []

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as exc:
        return [f"Không mở được file: {exc}"], []

    if not raw.strip():
        return ["File rỗng."], []

    lines = raw.splitlines()
    data_lines = [l for l in lines if l.strip() and not l.lstrip().startswith("#")]

    if not data_lines:
        return ["File không có dòng cookie nào, chỉ toàn chú thích."], []

    # Lỗi phổ biến nhất: copy qua chỗ nào đó biến TAB thành dấu cách. Định dạng
    # Netscape bắt buộc TAB, mất TAB là yt-dlp đọc ra rỗng mà không báo gì.
    tabbed = [l for l in data_lines if "\t" in l]
    if not tabbed:
        problems.append(
            "Không dòng nào có ký tự TAB. Định dạng Netscape bắt buộc ngăn cách "
            "bằng TAB — nhiều khả năng lúc copy đã bị đổi TAB thành dấu cách. "
            "Hãy mở file bằng trình soạn thảo và copy lại, đừng copy từ cửa sổ "
            "xem trước của trình duyệt."
        )
        return problems, notes

    bad_shape = [l for l in tabbed if len(l.split("\t")) != 7]
    if bad_shape:
        problems.append(
            f"{len(bad_shape)} dòng không đủ 7 cột. File có thể đã bị cắt xén."
        )

    domains, names, expiries = set(), set(), []
    for line in tabbed:
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _, _, _, expiry, name, _ = parts
        domains.add(domain.lstrip("."))
        names.add(name)
        try:
            value = int(expiry)
            if value > 0:
                expiries.append(value)
        except ValueError:
            pass

    yt = {d for d in domains if "youtube.com" in d or "google.com" in d}
    if not yt:
        problems.append(
            "Không có cookie nào của youtube.com hay google.com. "
            f"File này là của: {', '.join(sorted(domains)[:5])}"
        )

    found_auth = names & AUTH_COOKIES
    if not found_auth:
        problems.append(
            "Thiếu toàn bộ cookie đăng nhập (SID, HSID, SAPISID, __Secure-1PSID…). "
            "Nhiều khả năng bạn xuất cookie lúc chưa đăng nhập, hoặc xuất từ cửa "
            "sổ ẩn danh. Đăng nhập rồi xuất lại."
        )
    else:
        notes.append(f"Có {len(found_auth)} cookie đăng nhập: {', '.join(sorted(found_auth))}")

    now = time.time()
    live = [e for e in expiries if e > now]
    dead = [e for e in expiries if e <= now]
    if expiries and not live:
        problems.append("Mọi cookie trong file đều đã hết hạn.")
    elif live:
        soonest = min(live)
        days = (soonest - now) / 86400
        notes.append(
            f"Cookie sớm hết hạn nhất còn {days:.0f} ngày "
            f"({time.strftime('%d/%m/%Y', time.localtime(soonest))})."
        )
        if dead:
            notes.append(f"{len(dead)} cookie đã hết hạn (bình thường, bỏ qua được).")

    notes.append(f"Tổng cộng {len(tabbed)} cookie, {len(domains)} tên miền.")
    return problems, notes


def main():
    if len(sys.argv) != 2:
        print("Dùng: python3 scripts/check_cookies.py <cookies.txt>", file=sys.stderr)
        return 2

    problems, notes = check(sys.argv[1])
    for note in notes:
        print(f"  · {note}")
    if problems:
        print("\nFile cookie có vấn đề:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1
    print("\nFile cookie trông ổn.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
