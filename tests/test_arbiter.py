"""Сценарии режиссёра. Каждый — одно правило и поломка, от которой оно защищает."""


def owner_talks(arb, clock, seconds, step=0.1):
    """Стример говорит `seconds` секунд: VAD шлёт кадры, время идёт."""
    for _ in range(int(seconds / step)):
        arb.note_owner_voice()
        clock.tick(step)


def events(arb, kind):
    return [r for r in arb.journal.records if r["e"] == kind]


def test_two_phases_grant_commit_close(make, room):
    arb = make()
    lease = arb.request("caster", 7, max_audio_s=8)
    assert lease and lease.commit()
    lease.note_played("Вот это заход.")
    lease.close()
    assert arb.open_leases() == 0
    assert [r["e"] for r in arb.journal.records[1:]] == ["grant", "commit", "said"]


def test_moment_expires_between_request_and_sound(make, clock):
    """Одна фаза = TOCTOU: разрешение выдано под момент, которого к звуку уже нет."""
    arb = make()
    lease = arb.request("caster", 7, ttl=9, max_audio_s=8)
    clock.tick(10)                               # генерация затянулась
    assert not lease.commit()
    assert events(arb, "cancel")[-1]["why"] == "момент протух"
    assert arb.open_leases() == 0


def test_owner_opened_mouth_on_event_takes_the_moment(make, clock):
    arb = make()
    event_time = clock()
    lease = arb.request("caster", 7, event_time=event_time, max_audio_s=8)
    clock.tick(0.5)
    owner_talks(arb, clock, 1.0)                 # стример сам среагировал
    assert not lease.commit()
    assert events(arb, "cancel")[-1]["why"] == "стример сказал сам"


def test_continuous_reportage_is_not_a_reaction(make, clock):
    """Разбор 09.09: при сплошном репортаже совпадение речи с событием случайно.
    Старое правило (любая речь рядом) убивало реплику, новое (открыл рот в окне) — нет."""
    for rule, expected in (("opened_mouth", True), ("any_speech", False)):
        arb = make(owner_rule=rule)
        owner_talks(arb, clock, 3.0)             # говорит ДО события
        event_time = clock()
        owner_talks(arb, clock, 1.0)             # и продолжает про своё
        lease = arb.request("caster", 7, event_time=event_time, max_audio_s=8)
        assert bool(lease) is expected, rule
        if lease:
            lease.close()
        clock.tick(30)


def test_patient_lease_waits_for_a_gap_instead_of_dying(make, clock):
    arb = make()
    owner_talks(arb, clock, 1.0)
    lease = arb.request("director", 5, max_audio_s=7, patient=True)
    assert lease, "речь стримера — повод говорить, а не причина молчать"

    def sleep(seconds):
        clock.tick(seconds)                      # стример замолчал — фальшивое время просто идёт
    assert lease.wait_for_gap(sleep=sleep)
    assert lease.commit()
    lease.close()


def test_patient_lease_gives_up_if_owner_never_pauses(make, clock):
    arb = make(patience_sec=2.0)
    lease = arb.request("director", 5, max_audio_s=7, patient=True)
    owner_talks(arb, clock, 0.2)

    def sleep(seconds):
        arb.note_owner_voice()                   # говорит без пауз
        clock.tick(seconds)
    assert not lease.wait_for_gap(sleep=sleep)


def test_budget_counts_reserves_of_open_leases(make):
    """Три слоя думают одновременно — без учёта резерва все дружно пролезли бы в остаток."""
    arb = make(budget_sec=30, guard_sec=7, game_reserve_sec=0, min_gap_sec=0)
    first = arb.request("director", 5, max_audio_s=12)
    second = arb.request("chat", 5, max_audio_s=12)
    assert first and not second
    assert events(arb, "deny")[-1]["why"] == "бюджет речи"
    first.close()
    assert arb.request("chat", 5, max_audio_s=12)


def test_budget_free_shrinks_with_what_was_actually_said(make, room):
    arb = make(budget_sec=30, guard_sec=7, game_reserve_sec=0)
    assert arb.budget_free("director") == 23
    room.say(17.5)
    assert round(arb.budget_free("director"), 1) == 5.5


def test_game_reserve_only_while_game_is_on(make, clock):
    arb = make(budget_sec=30, guard_sec=7, game_reserve_sec=8, min_gap_sec=0)
    assert arb.budget_free("director") == 23              # игра не идёт — резерв не держим
    arb.request("caster", 5, max_audio_s=1).close()       # игровой слой ожил
    assert arb.budget_free("director") == 15
    assert arb.budget_free("caster") == 23
    clock.tick(121)
    assert arb.budget_free("director") == 23


def test_critical_event_bypasses_budget(make, room):
    """Разбор 09.09: победа в катке (вес 10) утонула в отказе «бюджет речи»."""
    room.say(25)
    assert make(critical_bypasses_budget=True).request("caster", 10, max_audio_s=8)
    assert not make(critical_bypasses_budget=False).request("caster", 10, max_audio_s=8)


def test_donation_is_never_interrupted(make, room):
    arb = make()
    room.speaking, room.current_source = True, "donation"
    assert not arb.request("caster", 10, max_audio_s=8)
    assert events(arb, "deny")[-1]["why"] == "играет донат"


def test_critical_interrupts_lighter_speech_only_when_its_sound_is_ready(make, room):
    arb = make(min_gap_sec=0)
    talk = arb.request("director", 5, max_audio_s=7)
    assert talk.commit()
    room.speaking, room.current_source = True, "director"
    ace = arb.request("caster", 10, max_audio_s=8)
    assert ace and room.barged == 0              # решение принято, но звук ещё не готов
    assert ace.commit()
    assert room.barged == 1                      # обрыв — в тот миг, когда преемнику есть что сказать


def test_same_weight_does_not_interrupt(make, room):
    arb = make(min_gap_sec=0)
    arb.request("caster", 9, max_audio_s=8).commit()
    room.speaking, room.current_source = True, "caster"
    assert not arb.request("caster", 9, max_audio_s=8)


def test_epoch_change_voids_the_thought(make):
    arb = make()
    lease = arb.request("director", 5, max_audio_s=7, epoch="round")
    arb.bump_epoch("round", "новый раунд")
    assert not lease.commit()


def test_dedupe_after_something_was_said(make, clock):
    arb = make(min_gap_sec=0)
    lease = arb.request("chat", 5, max_audio_s=5, dedupe_key="донат-Петя")
    lease.commit()
    lease.close()
    assert not arb.request("caster", 5, max_audio_s=5, dedupe_key="донат-Петя")
    clock.tick(91)
    assert arb.request("caster", 5, max_audio_s=5, dedupe_key="донат-Петя")


def test_min_gap_between_lines(make, room):
    arb = make(min_gap_sec=5)
    room.say(3)
    assert not arb.request("chat", 5, max_audio_s=3)
    assert events(arb, "deny")[-1]["why"] == "рано после прошлой реплики"


def test_context_manager_closes_on_error(make):
    """Забытая аренда навсегда съедает бюджет — бот медленно и молча затыкается."""
    arb = make()
    try:
        with arb.request("director", 5, max_audio_s=7):
            raise RuntimeError("модель упала посреди генерации")
    except RuntimeError:
        pass
    assert arb.open_leases() == 0


def test_journal_explains_every_silence(make, room):
    arb = make(min_gap_sec=5)
    room.say(2)
    arb.request("chat", 5, max_audio_s=3)
    deny = events(arb, "deny")[-1]
    assert deny["src"] == "chat" and deny["why"] and "used" in deny
