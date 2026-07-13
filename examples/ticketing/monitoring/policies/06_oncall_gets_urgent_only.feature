Feature: on-call discipline

  Scenario: the on-call agent only receives urgent tickets
    When a ticket is assigned to "oncall"
    Then a ticket priority is "urgent" before
