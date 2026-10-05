package com.example.sandbox

import io.mockk.coEvery
import io.mockk.mockk
import kotlinx.coroutines.test.runTest
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class GetUserUseCaseFailingTest {

    private val repository: UserRepository = mockk()
    private val useCase = GetUserUseCase(repository)

    @Test
    fun `invoke returns the user name`() = runTest {
        coEvery { repository.getUser("42") } returns
            User(id = "42", name = "Ada", email = "ada@example.com")

        // Intentional bug: wrong expected value.
        assertEquals("Grace", useCase("42").name)
    }
}
