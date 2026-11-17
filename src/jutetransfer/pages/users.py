"""Users: who may use the transfer app and who is a transfer admin.

Admins only. Writes one table, jt_user_access (see access.py); people are
picked from the VoWERP user list, never created here.
"""

import pandas as pd
import streamlit as st

from .. import access
from ..auth import is_admin


def users_page() -> None:
    st.title("Users")
    if not is_admin():
        st.info("Only a transfer admin can open this page.")
        return
    if access.legacy_login_enabled():
        st.warning("The old shared logins (admin / user) are still switched on "
                   "(JT_LEGACY_LOGIN). While they are, anyone with those passwords "
                   "can get in.")
    if not access.table_exists():
        st.error("The access list table (jt_user_access) is not created yet. "
                 "Until it is, only the bootstrap admins in JT_ADMIN_EMAILS can log in.")
        return

    me = st.session_state.get("user_id")
    st.caption("**user**: create chains, marked moves and lots.  "
               "**admin**: also delete or change what is saved, and manage this list.")

    rows = access.list_access()
    if rows:
        df = pd.DataFrame(rows)
        df["status"] = df.apply(
            lambda r: "No access" if not r["active"]
            else ("VoWERP login inactive" if not r["erp_active"] else "Active"), axis=1)
        st.dataframe(df[["name", "email_id", "role", "status"]].rename(
            columns={"name": "Name", "email_id": "E-mail", "role": "Role", "status": "Status"}),
            use_container_width=True, hide_index=True)
    else:
        st.info("Nobody is on the access list yet.")
    boot = sorted(access.admin_emails())
    if boot:
        st.caption("Always admin (set on the server): " + ", ".join(boot))

    st.subheader("Add or change a person")
    q = st.text_input("Search VoWERP users by name or e-mail", key="jt_users_search")
    found = access.find_users(q)
    if q and len(q.strip()) >= 2 and not found:
        st.caption("No active VoWERP user matches.")
    if found:
        labels = {f"{u['name']} ({u['email_id']})": u["user_id"] for u in found}
        c1, c2, c3 = st.columns([3, 1, 1])
        with c1:
            who = st.selectbox("Person", options=list(labels.keys()), key="jt_users_pick")
        with c2:
            role = st.selectbox("Role", options=list(access.ROLES), key="jt_users_role")
        with c3:
            give = st.selectbox("Access", options=["Allowed", "Removed"], key="jt_users_active")
        if st.button("Save", key="jt_users_save"):
            target = labels[who]
            if target == me and (give == "Removed" or role != access.ROLE_ADMIN):
                st.error("You cannot remove your own admin access. Ask another admin.")
            else:
                try:
                    access.set_access(target, role, give == "Allowed", me)
                    st.success(f"Saved: {who} is now "
                               + (f"'{role}'." if give == "Allowed" else "without access."))
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
