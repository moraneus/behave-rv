Feature: session shape

  Scenario: a logout follows a login
    When a user is "logout"
    Then a user is "login_ok" before
