#!/usr/bin/env python3
"""Apply release-polish fixes to a built MyPCBench artifact tree.

This runner-only repository does not ship the app source or VM bake pipeline.
Use this script as the reproducible post-build step for the current artifact
tree before packaging/rebaking the OSS image.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path


JAMAICA_CHECK_IN = "2026-06-21"
JAMAICA_CHECK_OUT = "2026-06-26"
OLD_JAMAICA_CHECK_IN = "2026-05-03"
OLD_JAMAICA_CHECK_OUT = "2026-05-08"


def patch_text(path: Path, old: str, new: str) -> bool:
    if not path.exists():
        return False
    text = path.read_text()
    if new in text:
        return True
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one patch target, found {count}")
    path.write_text(text.replace(old, new))
    return True


def table_names(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute("select name from sqlite_master where type='table'")
    }


def patch_dinoco(db: Path) -> None:
    if not db.exists():
        return
    con = sqlite3.connect(db)
    tables = table_names(con)
    if "flights" not in tables:
        con.close()
        return
    con.execute(
        """
        update flights
           set departure_date = ?,
               arrival_date = ?,
               status = 'scheduled',
               checked_in = 0,
               last_modified_at = null
         where user_email = 'michael.scott@dundermifflin.com'
           and flight_number = 'DN1562'
           and confirmation_code = 'DN-83823'
        """,
        (JAMAICA_CHECK_IN, JAMAICA_CHECK_IN),
    )
    if "flight_alternatives" in tables:
        con.execute(
            """
            update flight_alternatives
               set departure_date = ?,
                   arrival_date = ?
             where flight_id = (
               select id from flights
                where flight_number = 'DN1562'
                  and confirmation_code = 'DN-83823'
             )
            """,
            (JAMAICA_CHECK_IN, JAMAICA_CHECK_IN),
        )
    con.commit()
    con.close()


def patch_cheskepdia(db: Path) -> None:
    if not db.exists():
        return
    con = sqlite3.connect(db)
    tables = table_names(con)
    if "bookings" not in tables:
        con.close()
        return
    con.execute(
        """
        update bookings
           set check_in = ?,
               check_out = ?,
               status = 'confirmed',
               created_at = '2026-06-06T12:00:00+00:00'
         where user_email = 'michael.scott@dundermifflin.com'
           and confirmation_code = 'SM-88431'
           and property_name = 'Sandals Montego Bay'
        """,
        (JAMAICA_CHECK_IN, JAMAICA_CHECK_OUT),
    )
    if "booking_messages" in tables:
        con.execute(
            """
            delete from booking_messages
             where booking_id in (
               select id from bookings
                where check_in = '2026-08-07'
                  and confirmation_code like 'CHK-%'
             )
            """
        )
    con.execute(
        """
        delete from bookings
         where check_in = '2026-08-07'
           and confirmation_code like 'CHK-%'
        """
    )
    con.commit()
    con.close()


def patch_calendar(db: Path) -> None:
    if not db.exists():
        return
    con = sqlite3.connect(db)
    tables = table_names(con)
    if "events" in tables:
        con.execute(
            """
            update events
               set start_at = ?,
                   end_at = ?,
                   status = 'tentative',
                   updated_at = '2026-06-06 12:00:00'
             where user_email = 'michael.scott@dundermifflin.com'
               and title like 'Sandals Montego Bay, Jamaica%'
            """,
            (JAMAICA_CHECK_IN, JAMAICA_CHECK_OUT),
        )
        con.execute(
            """
            update events
               set start_at = ?,
                   end_at = ?,
                   status = 'confirmed',
                   updated_at = '2026-06-06 12:00:00'
             where user_email = 'michael.scott@dundermifflin.com'
               and title = 'Flight to Sandals Montego Bay, Jamaica'
            """,
            (f"{JAMAICA_CHECK_IN}T06:00:00", f"{JAMAICA_CHECK_IN}T09:00:00"),
        )
    if "canonical_events" in tables:
        con.execute(
            """
            update canonical_events
               set start = ?,
                   end = ?
             where summary like 'Sandals Montego Bay, Jamaica%'
            """,
            (JAMAICA_CHECK_IN, JAMAICA_CHECK_OUT),
        )
        con.execute(
            """
            update canonical_events
               set start = ?,
                   end = ?
             where summary = 'Flight to Sandals Montego Bay, Jamaica'
            """,
            (f"{JAMAICA_CHECK_IN}T06:00:00", f"{JAMAICA_CHECK_IN}T09:00:00"),
        )
    con.commit()
    con.close()


def patch_etaxi(db: Path) -> None:
    if not db.exists():
        return
    con = sqlite3.connect(db)
    if "rides" not in table_names(con):
        con.close()
        return
    con.execute(
        """
        update rides
           set date = ?,
               pickup_time = ?,
               dropoff_time = ?,
               status = 'scheduled'
         where user_email = 'michael.scott@dundermifflin.com'
           and pickup_address like '1725 Slough Ave%'
           and dropoff_address like 'AVP%'
           and date = ?
        """,
        (
            JAMAICA_CHECK_IN,
            f"{JAMAICA_CHECK_IN}T03:45:00",
            f"{JAMAICA_CHECK_IN}T04:24:00",
            OLD_JAMAICA_CHECK_IN,
        ),
    )
    con.execute(
        """
        update rides
           set date = ?,
               pickup_time = ?,
               dropoff_time = ?,
               status = 'scheduled'
         where user_email = 'michael.scott@dundermifflin.com'
           and pickup_address like 'AVP%'
           and dropoff_address like '1725 Slough Ave%'
           and date = ?
        """,
        (
            JAMAICA_CHECK_OUT,
            f"{JAMAICA_CHECK_OUT}T10:00:00",
            f"{JAMAICA_CHECK_OUT}T10:32:00",
            OLD_JAMAICA_CHECK_OUT,
        ),
    )
    con.commit()
    con.close()


def patch_mail(db: Path) -> None:
    if not db.exists():
        return
    con = sqlite3.connect(db)
    if "emails" not in table_names(con):
        con.close()
        return
    rows = con.execute(
        """
        select id, subject, body, thread_id
          from emails
         where body like '%DN1562%'
            or body like '%SM-88431%'
            or subject like '%DN1562%'
            or subject like '%SM-88431%'
        """
    ).fetchall()
    for row_id, subject, body, thread_id in rows:
        subject = (subject or "").replace(OLD_JAMAICA_CHECK_IN, JAMAICA_CHECK_IN)
        subject = subject.replace(OLD_JAMAICA_CHECK_OUT, JAMAICA_CHECK_OUT)
        body = (body or "").replace(OLD_JAMAICA_CHECK_IN, JAMAICA_CHECK_IN)
        body = body.replace(OLD_JAMAICA_CHECK_OUT, JAMAICA_CHECK_OUT)
        thread_id = (thread_id or "").replace(OLD_JAMAICA_CHECK_IN, JAMAICA_CHECK_IN)
        thread_id = thread_id.replace(OLD_JAMAICA_CHECK_OUT, JAMAICA_CHECK_OUT)
        subject_normalized = re.sub(r"[^a-z0-9]+", " ", subject.lower()).strip()
        con.execute(
            """
            update emails
               set subject = ?,
                   body = ?,
                   date = ?,
                   subject_normalized = ?,
                   thread_id = ?
             where id = ?
            """,
            (subject, body, JAMAICA_CHECK_IN, subject_normalized, thread_id, row_id),
        )
    con.commit()
    con.close()


def patch_vaultbank(db: Path) -> None:
    if not db.exists():
        return
    con = sqlite3.connect(db)
    if "transactions" not in table_names(con):
        con.close()
        return
    con.execute(
        """
        update transactions
           set date = ?
         where description like '%Sandals Montego Bay%'
           and description like '%SM-88431%'
        """,
        (JAMAICA_CHECK_IN,),
    )
    con.commit()
    con.close()


def patch_seed_root(seed_root: Path) -> None:
    patch_dinoco(seed_root / "dinoco-airlines.sqlite")
    patch_cheskepdia(seed_root / "cheskepdia.sqlite")
    patch_calendar(seed_root / "hoolicalendar.sqlite")
    patch_calendar(seed_root / "worlds/scranton-office/hoolicalendar.sqlite")
    patch_etaxi(seed_root / "etaxi.sqlite")
    patch_mail(seed_root / "mail.sqlite")
    patch_vaultbank(seed_root / "vaultbank.sqlite")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Artifact root containing web-apps/ and generated_data/.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Optional runtime /data directory to patch.",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    patched = []
    if patch_text(
        root
        / "web-apps/apps/cheskepdia/.next/static/chunks/app/stays/[id]/page-c5066c333e93e375.js",
        'P(""),I(!0)},disabled:T,className:"mt-4 w-full rounded-xl bg-airbnb-coral py-3.5 text-base font-semibold text-white disabled:opacity-60",children:T?"Reserving...":"Reserve"})',
        'P(""),ed()},disabled:T,className:"mt-4 w-full rounded-xl bg-airbnb-coral py-3.5 text-base font-semibold text-white disabled:opacity-60",children:T?"Reserving...":"Reserve"})',
    ):
        patched.append("cheskepdia-reserve")
    if patch_text(
        root / "web-apps/apps/cheskepdia/.next/server/app/stays/[id]/page.js",
        'F(""),D(!0)},disabled:P,className:"mt-4 w-full rounded-xl bg-airbnb-coral py-3.5 text-base font-semibold text-white disabled:opacity-60",children:P?"Reserving...":"Reserve"})',
        'F(""),eo()},disabled:P,className:"mt-4 w-full rounded-xl bg-airbnb-coral py-3.5 text-base font-semibold text-white disabled:opacity-60",children:P?"Reserving...":"Reserve"})',
    ):
        patched.append("cheskepdia-server-reserve")
    if patch_text(
        root / "web-apps/apps/dinoco-airlines/.next/server/chunks/712.js",
        "return[-95,0,150,330].map((i,n)=>{let o=(0,a.mH)(e.departure_time,i);return c({...e,departure_time:o},t,r,n)})",
        "return[-95,0,150,690].map((i,n)=>{let o=(0,a.mH)(e.departure_time,i);return c({...e,departure_time:o},t,r,n)})",
    ):
        patched.append("dinoco-night-slot")

    for seed_root in [
        root / "generated_data/michael_scott",
        root
        / "web-apps/apps/lockedin/.next/standalone/generated_data/michael_scott",
    ]:
        if seed_root.exists():
            patch_seed_root(seed_root)
            patched.append(str(seed_root))

    if args.data_root and args.data_root.exists():
        patch_seed_root(args.data_root)
        patched.append(str(args.data_root))

    print("Applied OSS polish artifact fixes:")
    for item in patched:
        print(f"- {item}")


if __name__ == "__main__":
    main()
