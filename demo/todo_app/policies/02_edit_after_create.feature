Feature: edit discipline

  Scenario: an edit follows a create
    When a task is "edited"
    Then a task is "created" before
