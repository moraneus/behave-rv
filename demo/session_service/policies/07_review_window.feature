Feature: lockout review SLA

  Scenario: a locked account is reviewed within the window
    When a user is "locked"
    Then a user is "review" within "8" seconds
