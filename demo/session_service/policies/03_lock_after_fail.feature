Feature: lockout forensics

  Scenario: a lockout follows a failed attempt
    When a user is "locked"
    Then a user is "login_fail" previously
