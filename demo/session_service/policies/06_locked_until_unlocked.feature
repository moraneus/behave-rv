Feature: lockout interval

  Scenario: a user must not act while locked, until unlocked
    Given a user is "locked" until a user is "unlocked"
    Then a user is "action" never happens
