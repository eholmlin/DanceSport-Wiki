from dsr.load.wdsf import guess_style


def test_guess_style_recognizes_ballroom_as_standard():
    # Real bug: NDCA titles say "Ballroom" (never "Standard") for
    # International Standard multi-dance events, e.g. "Int'l Ballroom
    # (W,T,VW,F,Q)" -- missing from the original 4-keyword list meant
    # every such title (and, transitively, every single-dance title
    # under the same division) had style=None.
    assert guess_style("ProAm Open Gold JR MxAm Int'l Ballroom (W,T,VW,F,Q)") == "Standard"


def test_guess_style_still_recognizes_the_original_four_keywords():
    assert guess_style("Amateur Open 4/5-Dance J1 Int'l Latin (CC,S,R,PD,J)") == "Latin"
    assert guess_style("WDSF Standard") == "Standard"
    assert guess_style("Amer. Smooth Championship (W,T,F,VW)") == "Smooth"
    assert guess_style("PA Grand National SR Open SR3 Amer. Rhythm Championship (CC,R,SW,M)") == "Rhythm"


def test_guess_style_single_style_dances_need_no_disambiguation():
    # Real case: the vast majority of NDCA titles name one dance directly
    # with no style-category word at all -- Paso Doble/Jive/Samba are
    # Int'l Latin only, Quickstep is Int'l Standard only, Bolero/Mambo are
    # Amer. Rhythm only, so no "Int'l"/"Amer." marker is needed to know
    # which style they belong to.
    assert guess_style("L-C1 Newcomer Silver Int'l Paso Doble") == "Latin"
    assert guess_style("G-C2 Open Gold Jive") == "Latin"
    assert guess_style("L-A2 Cl. Full Bronze Amer. Mambo") == "Rhythm"
    assert guess_style("L-C2 CL Beg Bronze 1 Bolero") == "Rhythm"
    assert guess_style("G-J1 Closed Pre-Bronze Int'l Quickstep") == "Standard"
    assert guess_style("L-A3 Intermediate Silver Int'l Samba") == "Latin"


def test_guess_style_dual_style_dances_need_a_marker():
    # Real case: Waltz/Tango/Foxtrot/Viennese Waltz are danced in BOTH
    # International Standard and American Smooth under the same name, and
    # Cha Cha/Rumba are danced in both International Latin and American
    # Rhythm -- an explicit "Int'l"/"Amer." marker elsewhere in the title
    # is what says which.
    assert guess_style("L-C1 Open Interm Gold Int'l Foxtrot") == "Standard"
    assert guess_style("L-B1 Full Gold Amer. Waltz") == "Smooth"
    assert guess_style("L-A2 Newcomer Int'l Tango") == "Standard"
    assert guess_style("Single Dance Events L-C1 Full Gold Amer. Cha Cha") == "Rhythm"
    assert guess_style("Pt-Jr-Yh Solo Star Singles G-P2 Cl. Full Bronze Int'l Rumba") == "Latin"
    assert guess_style("AC-LPT2 Pre-Gold Int'l Viennese Waltz Final") == "Standard"


def test_guess_style_dual_style_dance_with_no_marker_stays_unclassified():
    # Genuinely ambiguous from the title alone -- neither "Int'l" nor
    # "Amer." appears, so guessing either way would be a coin flip. Stays
    # None rather than guessed at, per this function's own "best-effort"
    # contract (raw_title is the source of truth).
    assert guess_style("L-C1 Beginner Silver Tango") is None
    assert guess_style("L-B2 Rising Star Beginner Silver Waltz") is None


def test_guess_style_swing_needs_an_american_marker_specifically():
    # "Swing" (East Coast Swing) has no International equivalent at all --
    # only ever Amer. Rhythm.
    assert guess_style("L-C2 Op. Int. Bronze Amer. Swing") == "Rhythm"
    assert guess_style("L-C2 Open Intermediate Silver Swing") is None


def test_guess_style_excludes_specialty_dances_reusing_dual_style_names():
    # Real case a naive dance-name match would get wrong: Country Western
    # and Nightclub dances reuse Waltz/Two Step/Cha Cha/Rumba's names for
    # a genuinely different style family -- these must NOT be classified
    # as Standard/Smooth/Latin/Rhythm just because the dance name matches.
    assert guess_style("Country Western Single L-B1 Closed Int. Bronze C/W Waltz") is None
    assert guess_style("G-C2 Open Inter-Bronze C/W Two Step") is None
    assert guess_style("NC Tues Single Dance L-A3 CL Silver 1 C/W Cha Cha") is None
    assert guess_style("AC-B2 RS Full Bronze C/W Rumba") is None
    assert guess_style("L-C2 Open Full Silver Nightclub Two Step") is None
    assert guess_style("G-C1 Pre-Silver West Coast Swing") is None


def test_guess_style_excludes_dances_not_in_the_four_recognized_styles():
    # Real case: Salsa, Bachata, Hustle, Merengue, Peabody, and Argentine
    # Tango aren't part of NDCA's 4 recognized styles at all (Argentine
    # Tango is a distinct dance from Standard/Smooth Tango, not a variant
    # of it) -- correctly stay unclassified rather than forced into the
    # nearest-sounding bucket.
    assert guess_style("L-A3 Cl. Full Bronze Salsa") is None
    assert guess_style("L-A2 RS Cl. Interm. Bronze Bachata") is None
    assert guess_style("L-C2 Beginner Bronze Hustle") is None
    assert guess_style("L-A2 Int Bronze 1 Merengue") is None
    assert guess_style("L-B2 Op. Int. Gold OIG Peabody /P") is None
    assert guess_style("Argentine Tango Open Silver") is None


def test_guess_style_returns_none_for_a_completely_unrecognized_title():
    assert guess_style("Best of the Best AC-JR Int'l Cha Cha") == "Latin"  # sanity: still finds Cha Cha + Int'l
    assert guess_style("Formation Team Open Silver AD") is None
