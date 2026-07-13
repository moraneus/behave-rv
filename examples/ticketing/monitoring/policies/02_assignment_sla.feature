Feature: assignment SLA

  Scenario: an opened ticket is assigned within the window
    When a ticket is "opened"
    Then a ticket is "assigned" within "30" seconds
