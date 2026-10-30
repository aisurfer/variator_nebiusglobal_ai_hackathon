"""Title mentions without infra: whole titles as runs of words, ambiguous and template titles left out."""
from __future__ import annotations

from kb.causal.mentions import find_mentions, title_index


def test_a_whole_title_in_the_text_is_a_mention():
    idx = title_index([("jira.PLAT-34", "Allow an S3 export for large accounts"),
                       ("jira.PLAT-32", "Ship incremental loads for events (phase 2)")])
    text = ("Hello Harriet, here is what we delivered in October: allow an S3 export for large accounts; "
            "ship incremental loads for events (phase 2). Let me know.")
    assert sorted(e for e, _ in find_mentions(text, idx)) == ["jira.PLAT-32", "jira.PLAT-34"]


def test_a_part_of_a_title_or_of_a_word_is_not_a_mention():
    idx = title_index([("jira.PLAT-34", "Allow an S3 export for large accounts")])
    assert find_mentions("we allow an S3 export for large teams", idx) == []
    assert find_mentions("disallow an S3 export for large accountsX", idx) == []


def test_short_ambiguous_and_template_titles_are_left_out():
    titles = [("jira.A-1", "Fix login"), ("jira.A-2", "Support SFTP imports"), ("jira.A-3", "Support SFTP imports")]
    titles += [(f"jira.BIZ-{i}", f"Proposal for Client{i}") for i in range(6)]
    titles += [("jira.BIZ-99", "Proposal for the yearly renewal of the platform")]
    idx = title_index(titles)
    text = "fix login; support SFTP imports; the proposal for client3; proposal for the yearly renewal of the platform"
    assert [e for e, _ in find_mentions(text, idx)] == ["jira.BIZ-99"]  # 2 words, 2 tickets, a template; a long one
