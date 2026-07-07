Feature: checkpoint SLA

  Scenario: a started task reaches a checkpoint promptly
    When a task is "started"
    Then a task is "checkpoint" within "3" seconds
