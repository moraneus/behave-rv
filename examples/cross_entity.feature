Feature: out of fragment

  # This scenario relates two independent entity keys -- an order (order_id) and
  # a customer (customer_id). The v1 single-key fragment refuses it at compile
  # time rather than mis-evaluating it.
  Scenario: an order may only be paid after the customer is gold
    When an order is "paid"
    Then a customer is "gold" before
