Feature: arango-cypher-py TCK harness sample

  Scenario: Empty graph returns empty (sample)
    Given an empty graph
    When executing query:
      """
      MATCH (n:User) WHERE n.id = "does-not-exist" RETURN n.id
      """
    Then the result should be empty

