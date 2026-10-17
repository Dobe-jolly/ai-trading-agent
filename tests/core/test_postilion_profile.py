"""tools/postilion_profile.py: reads Postilion structured data and prints only what several accounts share.
All data invented."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("postilion_profile", ROOT / "tools" / "postilion_profile.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


def item(text: str) -> str:
    size = str(len(text))
    return f"{len(size)}{size}{text}"


def structured(**pairs: str) -> str:
    return "".join(item(k) + item(v) for k, v in pairs.items())


def test_postilion_structured_data_is_read_including_one_nested_level_and_xml():
    inner = structured(Reason="ACCOUNT NOT LINKED", Ref="12345678901")
    text = structured(HostRsp=inner, Node="ZIPIT", Meta="<Data><Code>9</Code></Data>")
    assert profile.parse_structured(structured(A="1", LongerKey="x" * 120)) == {"A": "1", "LongerKey": "x" * 120}
    assert profile.parse_structured("not structured") is None and profile.parse_structured("15ab") is None
    keys = profile.keys_of(text)
    assert keys["HostRsp.Reason"] == "ACCOUNT NOT LINKED" and keys["Node"] == "ZIPIT"
    assert "Meta<Data>" in keys and "Meta<Code>" in keys
    assert profile.keys_of("<Root><Item/></Root>") == {"<Root>": "", "<Item>": ""}


def test_only_values_shared_by_several_accounts_are_printed_and_long_digits_never():
    rows = []
    for n in range(4):  # four accounts share the message; one client's name and account appear once
        rows.append({"rsp_code_rsp": "30", "sink_node_name": "ZIPITsnk", "extended_rsp_code": "",
                     "additional_rsp_data": "", "who": n,
                     "structured_data_rsp": structured(Reason="ACCOUNT NOT LINKED", Acct=f"40400012345{n}")})
    rows.append({"rsp_code_rsp": "30", "sink_node_name": "ZIPITsnk", "extended_rsp_code": "", "additional_rsp_data": "",
                 "who": 9, "structured_data_rsp": structured(Reason="ONE CLIENT NAME", Acct="404000123459")})
    rows += [{"rsp_code_rsp": "30", "sink_node_name": "ZIPITsnk", "extended_rsp_code": "", "additional_rsp_data": "",
              "who": 20 + n, "structured_data_rsp": structured(SenderLastName="COMMONSURNAME", SenderNationalId="X1",
                                                               **{"Postilion:MetaData": structured(Reason="1")})}
             for n in range(4)]  # a surname four accounts share is still a person's detail
    rows += [{"rsp_code_rsp": "91", "sink_node_name": "EcoSnk", "extended_rsp_code": "9101", "who": w,
              "additional_rsp_data": "", "structured_data_rsp": ""} for w in (1, 1, 2, 3)]
    text = "\n".join(profile.summarise(rows, min_clients=3))
    assert "'ACCOUNT NOT LINKED': 4 legs, 4 accounts" in text and "ONE CLIENT NAME" not in text
    assert "40400012345" not in text and "Acct: on 5/9 (length 12, digits 5)" in text
    assert "SenderLastName: on 4/9" in text and "COMMONSURNAME" not in text and "MetaData" not in text
    assert "extended_rsp_code: filled on 4/4" in text and "'9101': 4 legs, 3 accounts" in text
    assert profile.safe("call 0772123456 or a@b.co") == "call <10 digits> or <email>"


def test_the_sample_query_reads_no_account_or_card_number():
    sql = profile.sample_sql(24, 60, ["30", "91"])
    assert "rsp_code_rsp IN ('30', '91')" in sql and "tran_postilion_originated = '0'" in sql
    selected = sql.split("SELECT")[-1].split("FROM")[0]
    assert "account" not in selected and "pan" not in selected and "who" in selected
    assert "rsp_code_rsp <> '00'" in profile.sample_sql(24, 60, [])


def test_values_inside_postilion_xml_are_profiled_under_the_same_rule():
    xml = ("<Limits><Limit><LimitName>B2W Daily</LimitName><Period>Daily</Period><OnlineTotalLimit>50000</OnlineTotalLimit>"
           "<RemOnlineTotalLimit>{rem}</RemOnlineTotalLimit></Limit><Limit><LimitName>ATM</LimitName></Limit></Limits>")
    rows = [{"rsp_code_rsp": "61", "sink_node_name": "NmbIntellect", "extended_rsp_code": "P173", "additional_rsp_data": "",
             "who": n, "structured_data_rsp": structured(RemainingAdvancedLimits=xml.format(rem=100 + n))} for n in range(4)]
    text = "\n".join(profile.summarise(rows, min_clients=3))
    assert "RemainingAdvancedLimits<LimitName>: on 4/4" in text and "'B2W Daily': 4 legs, 4 accounts" in text
    assert "'50000': 4 legs, 4 accounts" in text and "RemainingAdvancedLimits<LimitName>[2]" in text
    assert "RemainingAdvancedLimits<RemOnlineTotalLimit>: on 4/4 (length 3, digits 4)" in text and "'101'" not in text
