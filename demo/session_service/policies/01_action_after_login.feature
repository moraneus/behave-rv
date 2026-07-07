Feature: authentication

  Scenario: an action requires a prior successful login
    When a user is "action"
    Then a user is "login_ok" before
