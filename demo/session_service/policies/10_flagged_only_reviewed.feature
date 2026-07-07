Feature: abuse quarantine

  Scenario: a flagged user is only reviewed afterwards
    Then a user is "review" since a user is "flagged"
