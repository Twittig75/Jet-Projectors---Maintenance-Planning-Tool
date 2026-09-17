import os
import tempfile
import threading
import streamlit as st

import db
import ocr_pipeline
import export

st.set_page_config(page_title="Hangar - Aircraft Records", page_icon="🛩️", layout="wide")
db.init_db()


def _process_file_in_background(job_id, aircraft_id, tmp_path, filename):
    """
    Runs in its own thread, independent of any browser session or web
    request. This is deliberate: a long OCR job tied to a single request has
    to finish inside whatever time limit the hosting platform enforces on
    that request/connection, and a large enough file can outlast that limit
    no matter how responsive the progress reporting is. Running the actual
    work here instead means it keeps going - and keeps saving results to the
    database as it completes - even if the browser tab that started it is
    closed, reloaded, or times out.
    """
    try:
        result = ocr_pipeline.process_file(
            tmp_path,
            progress_callback=lambda page, total: db.update_job_progress(job_id, page, total),
        )

        added_stc, added_337 = 0, 0
        for stc_num, data in result['stcs'].items():
            if not db.stc_exists(aircraft_id, stc_num):
                pages_str = ','.join(str(p) for p in data['pages'])
                db.add_stc_record(aircraft_id, stc_num, data['holder'], data['description'].strip(),
                                   filename, pages_str)
                added_stc += 1

        for rec in result['form337s']:
            pages_str = ','.join(str(p) for p in rec['pages'])
            db.add_337_record(aircraft_id, rec['reg_mark'], rec['work_date'], rec['summary'],
                               ','.join(rec['stc_refs']), filename, pages_str)
            added_337 += 1

        for flag in result['flagged']:
            db.add_review_flag(aircraft_id, filename, flag['page'], flag['reason'])

        for ev in result['registration_events']:
            date_parsed = ocr_pipeline.parse_date_str(ev['date_str'])
            db.add_registration_event(
                aircraft_id, ev['reg_mark'], ev['date_str'],
                date_parsed.isoformat() if date_parsed else None,
                ev['confidence'], filename, ev['page'],
            )

        db.refresh_tail_from_registration(aircraft_id)
        db.finish_job(job_id, added_stc=added_stc, added_337=added_337)
    except Exception as e:
        db.finish_job(job_id, error_message=str(e))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

if 'view' not in st.session_state:
    st.session_state.view = 'hangar'
if 'current_aircraft_id' not in st.session_state:
    st.session_state.current_aircraft_id = None


def go_hangar():
    st.session_state.view = 'hangar'
    st.session_state.current_aircraft_id = None


def go_aircraft(aid):
    st.session_state.view = 'aircraft'
    st.session_state.current_aircraft_id = aid


# ======================================================================
# HANGAR HOME PAGE
# ======================================================================
def render_hangar():
    st.title("🛩️ Hangar")
    st.caption("Aircraft under management. Click a tile to open its STC / 337 records.")

    with st.expander("➕ Add aircraft"):
        st.caption("Tail number is just a starting label — once you drop in records, "
                   "it auto-updates to the most recent registration found on file.")
        with st.form("add_aircraft_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            tail = c1.text_input("Tail number (e.g. N284EM)")
            model = c2.text_input("Make / Model (optional)")
            serial = c3.text_input("Serial number (optional)")
            submitted = st.form_submit_button("Add to Hangar")
            if submitted:
                if tail.strip():
                    db.add_aircraft(tail.strip().upper(), model.strip(), serial.strip())
                    st.success(f"Added {tail.strip().upper()}")
                    st.rerun()
                else:
                    st.error("Tail number is required.")

    st.divider()

    aircraft_list = db.list_aircraft()
    if not aircraft_list:
        st.info("No aircraft yet. Add one above to get started.")
        return

    cols = st.columns(3)
    for i, ac in enumerate(aircraft_list):
        stc_count = len(db.get_stc_records(ac['id']))
        f337_count = len(db.get_337_records(ac['id']))
        flag_count = len(db.get_review_flags(ac['id']))
        active_count = len(db.get_active_jobs(ac['id']))
        with cols[i % 3]:
            with st.container(border=True):
                st.subheader(ac['tail_number'])
                if ac['make_model']:
                    st.caption(ac['make_model'])
                if ac['serial_number']:
                    st.caption(f"S/N {ac['serial_number']}")
                m1, m2 = st.columns(2)
                m1.metric("STCs", stc_count)
                m2.metric("337s", f337_count)
                if active_count:
                    st.info(f"⚙️ Processing {active_count} file(s) in the background...")
                if flag_count:
                    st.warning(f"{flag_count} page(s) flagged for review")
                bcol1, bcol2 = st.columns([3, 1])
                if bcol1.button("Open ➜", key=f"open_{ac['id']}", use_container_width=True):
                    go_aircraft(ac['id'])
                    st.rerun()
                if bcol2.button("🗑️", key=f"del_{ac['id']}", help="Remove aircraft"):
                    db.delete_aircraft(ac['id'])
                    st.rerun()


# ======================================================================
# AIRCRAFT DETAIL PAGE
# ======================================================================
def render_aircraft():
    aid = st.session_state.current_aircraft_id
    ac = db.get_aircraft(aid)
    if ac is None:
        st.error("Aircraft not found.")
        if st.button("Back to Hangar"):
            go_hangar()
            st.rerun()
        return

    top1, top2 = st.columns([1, 6])
    with top1:
        if st.button("⬅ Hangar"):
            go_hangar()
            st.rerun()
    with top2:
        st.title(ac['tail_number'])
        sub = ' | '.join(filter(None, [ac['make_model'], f"S/N {ac['serial_number']}" if ac['serial_number'] else None]))
        if sub:
            st.caption(sub)

    reg_events = db.get_registration_events(aid)
    other_marks = sorted(set(e['reg_mark'] for e in reg_events if e['reg_mark'] != ac['tail_number']))
    if reg_events:
        note = "Tail number is auto-set to the most recently dated registration found in uploaded records."
        if other_marks:
            note += f" Also seen in this aircraft's history: {', '.join(other_marks)}."
        st.caption(f"ℹ️ {note}")
        with st.expander("📜 Registration history found in records"):
            st.dataframe(
                [{"Registration": e['reg_mark'], "Date": e['date_str'] or '(unreadable)',
                  "Confidence": e['confidence'], "Source": f"{e['source_file']} (p.{e['source_page']})"}
                 for e in reg_events],
                use_container_width=True, hide_index=True,
            )

    st.divider()

    # ---------------- Drop zone ----------------
    st.subheader("📥 Add records")
    st.write("Drag and drop scanned STC/337 files (PDF, PNG, JPG, TIFF) below. "
             "Each file is OCR'd and scanned automatically; new STCs and 337s are added to the lists. "
             "Processing runs in the background — it's safe to navigate away or close this tab, "
             "and large files won't be interrupted by a page timeout.")
    uploaded = st.file_uploader(
        "Drop files here",
        type=["pdf", "png", "jpg", "jpeg", "tif", "tiff"],
        accept_multiple_files=True,
        key=f"uploader_{aid}",
        label_visibility="collapsed",
    )

    if uploaded:
        dispatched_key = f"dispatched_{aid}"
        if dispatched_key not in st.session_state:
            st.session_state[dispatched_key] = set()

        new_files = [f for f in uploaded if f.name not in st.session_state[dispatched_key]]
        for f in new_files:
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(f.name)[1]) as tmp:
                tmp.write(f.read())
                tmp_path = tmp.name

            job_id = db.create_job(aid, f.name)
            thread = threading.Thread(
                target=_process_file_in_background,
                args=(job_id, aid, tmp_path, f.name),
                daemon=True,
            )
            thread.start()
            st.session_state[dispatched_key].add(f.name)

        if new_files:
            st.success(f"Started processing {len(new_files)} file(s) in the background. "
                       "Progress shows below — check back anytime, this page doesn't need to stay open.")
            st.rerun()

    # ---------------- Background job status ----------------
    active_jobs = db.get_active_jobs(aid)
    recent_jobs = [j for j in db.get_jobs(aid, limit=10) if j['status'] != 'processing']

    if active_jobs or recent_jobs:
        st.divider()
        st.subheader("⚙️ Processing status")
        if st.button("🔄 Refresh status"):
            st.rerun()

        for job in active_jobs:
            total = job['total_pages'] or 1
            frac = min(job['current_page'] / total, 1.0) if total else 0.0
            st.write(f"**{job['filename']}** — processing page {job['current_page']} of {job['total_pages'] or '?'}")
            st.progress(frac)

        for job in recent_jobs[:5]:
            if job['status'] == 'done':
                st.caption(f"✅ {job['filename']}: +{job['added_stc']} STC(s), +{job['added_337']} 337(s)")
            elif job['status'] == 'error':
                st.caption(f"⚠️ {job['filename']}: failed — {job['error_message']}")

        if active_jobs:
            st.caption("This page updates when you click Refresh — feel free to check back later instead of waiting here.")

    st.divider()

    stc_records = db.get_stc_records(aid)
    f337_records = db.get_337_records(aid)
    flags = db.get_review_flags(aid)

    # ---------------- STC Listing ----------------
    st.subheader(f"📋 STC Listing ({len(stc_records)})")
    if stc_records:
        st.dataframe(
            [{"STC Number": r['stc_number'], "Holder": r['holder'] or '(unreadable)',
              "Description": (r['description'] or '')[:120] + ('...' if len(r['description'] or '') > 120 else ''),
              "Source": f"{r['source_file']} (p.{r['source_pages']})"} for r in stc_records],
            use_container_width=True, hide_index=True,
        )
        c1, c2 = st.columns(2)
        c1.download_button("⬇ Download STC List (PDF)", export.stc_listing_pdf(ac, stc_records),
                            file_name=f"{ac['tail_number']}_STC_Listing.pdf", mime="application/pdf",
                            use_container_width=True)
        c2.download_button("⬇ Download STC List (XLS)", export.stc_listing_xlsx(ac, stc_records),
                            file_name=f"{ac['tail_number']}_STC_Listing.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True)
    else:
        st.caption("No STC records yet — drop in some scanned files above.")

    st.divider()

    # ---------------- 337 Listing ----------------
    st.subheader(f"📋 Form 337 Listing ({len(f337_records)})")
    if f337_records:
        st.dataframe(
            [{"Reg. Mark": r['reg_mark'], "Date": r['work_date'],
              "Summary": (r['summary'] or '')[:120] + ('...' if len(r['summary'] or '') > 120 else ''),
              "STCs Referenced": r['stc_refs'] or '-',
              "Source": f"{r['source_file']} (p.{r['source_pages']})"} for r in f337_records],
            use_container_width=True, hide_index=True,
        )
        c1, c2 = st.columns(2)
        c1.download_button("⬇ Download 337 List (PDF)", export.form337_listing_pdf(ac, f337_records),
                            file_name=f"{ac['tail_number']}_337_Listing.pdf", mime="application/pdf",
                            use_container_width=True)
        c2.download_button("⬇ Download 337 List (XLS)", export.form337_listing_xlsx(ac, f337_records),
                            file_name=f"{ac['tail_number']}_337_Listing.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True)
    else:
        st.caption("No Form 337 records yet — drop in some scanned files above.")

    if flags:
        st.divider()
        with st.expander(f"⚠️ {len(flags)} page(s) flagged for manual review"):
            for fl in flags:
                st.write(f"**{fl['source_file']}**, page {fl['page']}: {fl['reason']}")


# ======================================================================
if st.session_state.view == 'hangar':
    render_hangar()
else:
    render_aircraft()
