Feature: conversation SLA

  Scenario: a customer reply is answered within the window
    When a customer reply arrives
    Then an agent reply is sent within "60" seconds
