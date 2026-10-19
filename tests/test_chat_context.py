"""B13: a chat reply uses the facts of the resume the application was sent with."""
import json

import chat_context as cc

QA_HASH = "a" * 38
EL_HASH = "b" * 38


def make_profiles(tmp_path):
    root = tmp_path / "profiles"
    qa = root / "qa"
    (qa / "directions").mkdir(parents=True)
    (qa / "directions" / "d_qa.json").write_text(json.dumps({"name": "d_qa", "title": "QA", "variants": [
        {"hh_resume_hash": QA_HASH, "hh_resume_numeric_id": 111, "title": "QA Engineer"}]}))
    (qa / "directions" / "d_el.json").write_text(json.dumps({"name": "d_el", "title": "Электрик", "variants": [
        {"hh_resume_hash": EL_HASH, "hh_resume_numeric_id": 222, "title": "Электромонтажник"}]}))
    el = root / "electrician"
    el.mkdir()
    (el / "profile.env").write_text(f"HH_PRIMARY_RESUME_ID={EL_HASH}\nHH_PRIMARY_RESUME_TITLE=Электромонтажник\n")
    (root / "qa-dev").mkdir()
    (root / "qa-dev" / "directions").mkdir()
    (root / "qa-dev" / "directions" / "d_qa.json").write_text((qa / "directions" / "d_qa.json").read_text())
    snapshot = "\n".join(json.dumps(r) for r in [{"chatId": 1, "resumeId": 111}, {"chatId": 2, "resumeId": 222},
                                                    {"kind": "snapshot_summary"}, {"chatId": 3, "resumeId": 999}])
    for home in (qa, el):
        (home / "negotiation_snapshots.jsonl").write_text(snapshot)
    return qa, el


def test_own_chat_maps_to_its_direction(tmp_path):
    qa, el = make_profiles(tmp_path)
    assert cc.resolve(str(qa), "1") == cc.ChatOwner("own", "111", "d_qa", "QA Engineer")
    assert cc.resolve(str(qa), "2").direction == "d_el"


def test_legacy_profile_knows_its_resume_through_any_direction_on_the_machine(tmp_path):
    qa, el = make_profiles(tmp_path)
    assert cc.resolve(str(el), "2") == cc.ChatOwner("own", "222", "", "Электромонтажник")


def test_chat_of_another_profile_is_refused_and_names_the_real_owner(tmp_path):
    qa, el = make_profiles(tmp_path)
    owner = cc.resolve(str(el), "1")
    assert owner.status == "other" and owner.owner_profile == "qa"  # not the qa-dev copy
    assert "ответьте из профиля qa" in cc.refusal_text(owner, "electrician")
    assert cc.resolve(str(el), "3").status == "other"  # a resume nobody here owns


def test_unknown_without_snapshot_record(tmp_path):
    qa, el = make_profiles(tmp_path)
    assert cc.resolve(str(qa), "404").status == "unknown"
    assert cc.resolve(str(tmp_path / "nowhere"), "1").status == "unknown"
