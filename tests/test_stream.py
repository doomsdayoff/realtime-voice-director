from director.stream import speak, split_sentences


def test_sentences_are_cut_as_the_stream_arrives():
    chunks = ["Ну и ", "заход. Три", " фрага за 3.5 секунды! Дальше", "… посмотрим\nИ всё"]
    assert list(split_sentences(chunks)) == [
        "Ну и заход.", "Три фрага за 3.5 секунды!", "Дальше…", "посмотрим", "И всё",
    ]


def test_speak_commits_at_first_sound_and_records_only_what_played(make):
    arb = make()
    lease = arb.request("caster", 7, max_audio_s=8)
    played = []

    def play(audio):
        played.append(audio)
        return len(played) < 2                    # второе предложение оборвали (стоп, донат)

    said = speak(lease, ["Первое. ", "Второе. ", "Третье."], synthesize=str.upper, play=play)
    assert said == "Первое."                      # в память эфира — только прозвучавшее
    assert played == ["ПЕРВОЕ.", "ВТОРОЕ."]
    assert arb.open_leases() == 0


def test_speak_drops_the_line_if_moment_expired_before_first_sound(make, clock):
    arb = make()
    lease = arb.request("caster", 7, ttl=9, max_audio_s=8)

    def slow_synth(text):
        clock.tick(10)                            # синтез первого предложения не успел
        return text

    assert speak(lease, ["Поздно. "], synthesize=slow_synth, play=lambda a: True) == ""
    assert arb.open_leases() == 0


def test_started_line_is_not_cut_by_ttl(make, clock):
    """Оборванная на полуслове мысль звучит хуже запоздавшей."""
    arb = make()
    lease = arb.request("caster", 7, ttl=9, max_audio_s=8)

    def play(audio):
        clock.tick(6)                             # к концу второго предложения срок годности прошёл
        return True

    assert speak(lease, ["Раз. ", "Два. "], synthesize=str, play=play) == "Раз. Два."
