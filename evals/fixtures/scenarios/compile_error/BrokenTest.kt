package com.example.sandbox

import io.mockk.mockk
import kotlinx.coroutines.test.runTest
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class BrokenTest {

    // Intentional bug: UserService does not exist.
    private val service: UserService = mockk()

    @Test
    fun `getUser is called with an Int id`() = runTest {
        val useCase = GetUserUseCase(mockk())

        // Intentional bug: getUser takes a String, not an Int.
        assertEquals("42", useCase(42).id)
    }
}
